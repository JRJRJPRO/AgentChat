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
        info = self.running.get(aid)
        if info and info.get("proc"):
            info["stopped"] = True
            try:
                info["proc"].kill()
            except ProcessLookupError:
                pass

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
        prompt = prompts.wake_prompt(agent, blocks, first)

        cfg_path = self._write_mcp_config(agent)
        preset = config.PERMISSION_PRESETS.get(agent["permission"], config.PERMISSION_PRESETS["worker"])
        allowed = ",".join(config.ALLOWED_CHAT_TOOLS + preset["extra_allowed"])

        cmd = self._claude_cmd() + [
            "-p", "--output-format", "text",
            "--model", agent["model"],
            "--permission-mode", preset["mode"],
            "--allowedTools", allowed,
            "--mcp-config", cfg_path, "--strict-mcp-config",
        ]
        cmd += ["--session-id", agent["session_id"]] if first else ["--resume", agent["session_id"]]

        os.makedirs(agent["cwd"], exist_ok=True)
        log_path = os.path.join(config.LOG_DIR, f"agent{aid}_{int(time.time())}.log")
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.git_bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = self.git_bash

        await self.broadcast({"t": "agent", "id": aid, "run": "working"})

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
        stopped = self.running.get(aid, {}).get("stopped")
        if rc == 0:
            self.fails[aid] = 0
            if first:
                db.update_agent(aid, bootstrapped=1)
        elif stopped:
            pass  # 用户手动停止：不算失败，也不重发（避免死循环）
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
