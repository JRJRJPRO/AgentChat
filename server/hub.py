"""Hub 调度器：AgentChat 的心脏。

工作方式（解决"agent 立即收到消息但等待时不烧 token"）：
- agent 平时不运行。每个 agent 对应一个持久的 claude 会话 id。
- 有新消息 → poke() 唤醒扫描循环 → 攒批(debounce) → 对每个有未派送消息的
  agent 启动一次 `claude -p --resume <会话id>`，把新消息喂进去。
- agent 用 chat MCP 工具回复/建群，回合结束进程退出，继续挂起。
- 期间来的新消息排队，本轮结束后再触发下一轮唤醒。

失败处理：唤醒失败回退派送游标（消息会重发），连续失败自动暂停该 agent
并在与用户的私聊里留言说明。
"""
import asyncio
import json
import os
import shutil
import sys
import time
import uuid

from . import config, db, prompts


class Hub:
    def __init__(self, broadcast):
        """broadcast: async 函数，把事件推给所有 UI WebSocket。"""
        self.broadcast = broadcast
        self.event = asyncio.Event()
        self.running = {}        # agent_id -> {"proc":..., "since":..., "stopped":bool}
        self.last_done = {}      # agent_id -> 上次唤醒结束时间（冷却用）
        self.fails = {}          # agent_id -> 连续失败次数
        self.interrupted = set() # 被打断过、下次唤醒要带"被打断"标注的 agent
        self.chain_notified = set()  # 已经广播过"链长暂停"的会话，避免刷屏
        self.claude = shutil.which("claude")
        self.git_bash = self._find_git_bash()  # Windows 上 claude 需要 git-bash

    @staticmethod
    def _find_git_bash():
        if os.name != "nt" or os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
            return None
        git = shutil.which("git")
        candidates = []
        if git:
            root = os.path.dirname(os.path.dirname(git))  # Git\cmd\git.exe → Git
            candidates += [os.path.join(root, "bin", "bash.exe"),
                           os.path.join(os.path.dirname(root), "bin", "bash.exe")]
        candidates += [r"C:\Program Files\Git\bin\bash.exe",
                       r"C:\Program Files (x86)\Git\bin\bash.exe"]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    # ---------- 对外接口 ----------

    def poke(self):
        self.event.set()

    def run_state(self, aid):
        return "working" if aid in self.running else "idle"

    async def stop_agent(self, aid):
        """硬停止：杀进程，这批消息不再重发。"""
        info = self.running.get(aid)
        if info and info.get("proc"):
            info["stopped"] = True
            try:
                info["proc"].kill()
            except ProcessLookupError:
                pass

    async def interrupt_agent(self, aid):
        """软打断：杀进程，但消息回退重发，下次唤醒带"你被打断了"标注，
        agent 借 --resume 的记忆接着干，不从头重来。"""
        info = self.running.get(aid)
        if not (info and info.get("proc")):
            return False
        info["interrupt"] = True
        try:
            info["proc"].kill()
        except ProcessLookupError:
            pass
        return True

    async def chain_clear(self, cid):
        if cid in self.chain_notified:
            self.chain_notified.discard(cid)
            await self.broadcast({"t": "chain", "conv_id": cid, "paused": False})

    # ---------- 主循环 ----------

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if self.event.is_set():
                self.event.clear()
                await asyncio.sleep(config.WAKE_DEBOUNCE)  # 攒一批
            for agent in db.list_agents():
                aid = agent["id"]
                if agent["status"] != "active" or aid in self.running:
                    continue
                if time.time() - self.last_done.get(aid, 0) < config.WAKE_COOLDOWN:
                    self.event.set()  # 冷却中，稍后再扫
                    continue
                pend = db.agent_pending(agent)
                for cid in pend["paused"]:
                    await self._notify_chain(cid)
                if pend["wake"]:
                    self.running[aid] = {"proc": None, "since": time.time(), "stopped": False}
                    asyncio.create_task(self._wake_wrapped(agent, pend))

    async def _notify_chain(self, cid):
        if cid not in self.chain_notified:
            self.chain_notified.add(cid)
            await self.broadcast({"t": "chain", "conv_id": cid, "paused": True})

    # ---------- 唤醒一个 agent ----------

    async def _wake_wrapped(self, agent, pend):
        aid = agent["id"]
        try:
            await self._wake(agent, pend)
        except Exception as e:
            await self._on_failure(agent, f"唤醒过程异常: {e}", pend, revert=True)
        finally:
            self.running.pop(aid, None)
            self.last_done[aid] = time.time()
            await self.broadcast({"t": "agent", "id": aid, "run": "idle"})
            self.event.set()  # 期间可能有新消息排队

    async def _wake(self, agent, pend):
        aid = agent["id"]
        if not self.claude:
            self.claude = shutil.which("claude")
            if not self.claude:
                raise RuntimeError("PATH 里找不到 claude CLI")

        # 推进派送游标（失败时回退）
        prev = {}
        for b in pend["batches"]:
            cid = b["conv"]["id"]
            prev[cid] = db.get_cursor(("agent", aid), cid)["last_delivered_id"]
            db.set_delivered(("agent", aid), cid, b["msgs"][-1]["id"])
        pend["prev_cursors"] = prev

        first = not agent["bootstrapped"]
        blocks = [prompts.batch_block(b["conv"], b["member_names"], b["msgs"]) for b in pend["batches"]]
        prompt = prompts.wake_prompt(agent, blocks, first, interrupted=aid in self.interrupted)
        self.interrupted.discard(aid)

        cfg_path = self._write_mcp_config(agent)
        preset = config.PERMISSION_PRESETS.get(agent["permission"], config.PERMISSION_PRESETS["worker"])
        allowed = ",".join(config.ALLOWED_CHAT_TOOLS + preset["extra_allowed"])

        cmd = self._claude_cmd() + [
            "-p", "--output-format", "json",  # json 里带每次唤醒的 token 用量
            "--model", config.MODEL_IDS.get(agent["model"], agent["model"]),
            "--permission-mode", preset["mode"],
            "--allowedTools", allowed,
            "--mcp-config", cfg_path, "--strict-mcp-config",
        ]
        for d in (agent.get("extra_dirs") or "").split(","):
            d = d.strip()
            if d:
                cmd += ["--add-dir", d]
        if agent.get("ask_perm"):
            # 越权操作不再直接拒绝，而是通过 MCP 工具弹到界面上等用户点允许/拒绝
            cmd += ["--permission-prompt-tool", "mcp__chat__ask_permission"]
        cmd += ["--session-id", agent["session_id"]] if first else ["--resume", agent["session_id"]]

        os.makedirs(agent["cwd"], exist_ok=True)
        self._ensure_claude_md(agent)
        log_path = os.path.join(config.LOG_DIR, f"agent{aid}_{int(time.time())}.log")
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MCP_TOOL_TIMEOUT"] = "660000"  # ask_permission 要等用户，别被默认超时掐断
        if self.git_bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = self.git_bash

        await self.broadcast({"t": "agent", "id": aid, "run": "working"})

        started = time.time()
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# cmd: {cmd}\n# ---- prompt ----\n{prompt}\n# ---- output ----\n")
            f.flush()
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=agent["cwd"], env=env,
                stdin=asyncio.subprocess.PIPE, stdout=f, stderr=f,
            )
            self.running[aid]["proc"] = proc
            self.running[aid]["log"] = log_path
            try:
                await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=config.WAKE_TIMEOUT)
                rc = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                rc = -1
                f.write("\n# !! 超时被杀\n")

        db.record_wake(aid)
        db.add_wake(aid, started, rc, self._parse_usage(log_path), log_path)
        stopped = self.running.get(aid, {}).get("stopped")
        interrupt = self.running.get(aid, {}).get("interrupt")
        if rc == 0:
            self.fails[aid] = 0
            if first:
                db.update_agent(aid, bootstrapped=1)
        elif stopped:
            # 用户硬停止：不算失败，也不重发（避免死循环）
            if first:
                db.update_agent(aid, session_id=str(uuid.uuid4()))  # 半截会话作废，防 session-id 冲突
        elif interrupt:
            # 软打断：回退游标让消息（连同打断者的新话）合并重发，标注"被打断"
            for cid, cur in prev.items():
                db.set_delivered(("agent", aid), cid, cur)
            self.interrupted.add(aid)
            self.fails[aid] = 0
            if first:
                db.update_agent(aid, session_id=str(uuid.uuid4()))
        else:
            if first:
                # 首次唤醒失败时会话可能已被占用，换一个新 session id 重来
                db.update_agent(aid, session_id=str(uuid.uuid4()))
            await self._on_failure(agent, f"claude 退出码 {rc}，日志: {log_path}", pend, revert=True)

    async def _on_failure(self, agent, why, pend, revert):
        aid = agent["id"]
        if revert:
            for cid, cur in (pend.get("prev_cursors") or {}).items():
                db.set_delivered(("agent", aid), cid, cur)
        self.fails[aid] = self.fails.get(aid, 0) + 1
        if self.fails[aid] >= config.MAX_CONSEC_FAILURES:
            db.update_agent(aid, status="paused")
            self.fails[aid] = 0
            dm = db.ensure_dm(db.USER, ("agent", aid))
            msg = db.post_message(dm["id"], "system", 0,
                                  f"「{agent['name']}」连续唤醒失败，已自动暂停。{why}", kind="error")
            await self.broadcast({"t": "msg", "conv_id": dm["id"], "message": msg})
            await self.broadcast({"t": "agent", "id": aid, "run": "idle"})

    # ---------- 工具 ----------

    @staticmethod
    def _ensure_claude_md(agent):
        """agent 的两级长期记忆：工作目录 CLAUDE.md（私有）+ @导入 shared/TEAM.md（全员共享）。
        CLAUDE.md 每次启动必定全文进上下文（无头模式也是），比 memory 目录可靠。
        只在缺失时生成模板，已有的绝不覆盖。"""
        path = os.path.join(agent["cwd"], "CLAUDE.md")
        if os.path.exists(path):
            return
        team = config.SHARED_TEAM_FILE.replace("\\", "/")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"# 「{agent['name']}」的长期记忆\n\n"
                f"@{team}\n\n"
                "上面一行导入了团队共享知识库（全员共用；要改共享内容请编辑那个文件本身）。\n"
                "从这里往下是只属于你的长期知识：职责总结、John 的相关偏好、踩坑经验。\n"
                "本文件每次唤醒都自动进入你的上下文，你可以随时自己编辑更新——保持精炼。\n"
            )

    @staticmethod
    def _parse_usage(log_path):
        """从 claude -p --output-format json 的输出里抠 token 用量。失败就算了，不影响主流程。"""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            payload = text.split("# ---- output ----\n", 1)[1].strip()
            data = json.loads(payload[payload.index("{"):])
            u = data.get("usage") or {}
            return {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "cost_usd": data.get("total_cost_usd", 0.0),
                "num_turns": data.get("num_turns", 0),
            }
        except Exception:
            return None

    def _claude_cmd(self):
        p = self.claude
        if p.lower().endswith((".cmd", ".bat")):  # npm 装的是 .cmd 壳，得借 cmd.exe 启动
            return ["cmd", "/c", p]
        return [p]

    def _write_mcp_config(self, agent):
        path = os.path.join(config.MCP_DIR, f"agent{agent['id']}.json")
        cfg = {
            "mcpServers": {
                "chat": {
                    "command": sys.executable,
                    "args": [os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_mcp.py")],
                    "env": {
                        "HUB_URL": config.HUB_URL,
                        "AGENT_TOKEN": agent["token"],
                        "PYTHONIOENCODING": "utf-8",
                    },
                }
            }
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        return path
