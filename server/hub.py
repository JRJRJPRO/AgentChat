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

from . import auth, config, db, memories, prompts, usage


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
        self.activity = {}       # agent_id -> 本轮唤醒的过程动态 [{ts,k,...}]（只进内存和界面，不进聊天记录）
        self.auth_needed = None  # 登录失效时 {"agent","detail","ts"}；置位期间暂停一切唤醒
        self.usage_alert = None  # Session(5h) 用量过阈值时 {"pct","resets_at","threshold"}
        self.usage_note_sent = set()  # 本轮预警期内已捎过收尾提醒的 agent_id
        self.usage_failing = False    # 用量接口连续查询失败（格式变了/网络断了），要让用户知道
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

    async def clear_auth(self):
        """用户在界面上点了"已登录，重试"：解除唤醒封锁并立刻补送积压消息。"""
        if self.auth_needed:
            self.auth_needed = None
            await self.broadcast({"t": "auth", "needed": False})
        self.fails.clear()
        self.poke()

    # ---------- 订阅用量预警（Session 5h） ----------

    async def usage_monitor(self):
        """后台轮询订阅用量（纯元数据接口，零 token 成本）。过阈值就置预警：
        正在干活的 agent 经工具捎带收到收尾提醒，新唤醒的写进唤醒词；
        窗口重置（resets_at 一变）自动解除。连续查询失败 3 次要让用户知道（接口格式可能变了）。"""
        fails = 0
        while True:
            row = await asyncio.to_thread(usage.session_usage)
            if row is None:
                fails += 1
                if fails >= 3 and not self.usage_failing:
                    self.usage_failing = True
                    await self.broadcast({"t": "usage_fail", "failing": True})
            else:
                fails = 0
                if self.usage_failing:
                    self.usage_failing = False
                    await self.broadcast({"t": "usage_fail", "failing": False})
                pct = row.get("utilization") or 0
                if pct >= self.usage_warn_pct():
                    fresh = not self.usage_alert or self.usage_alert.get("resets_at") != row.get("resets_at")
                    if fresh:
                        self.usage_note_sent.clear()
                    self.usage_alert = {"pct": pct, "resets_at": row.get("resets_at"),
                                        "threshold": self.usage_warn_pct()}
                    await self.broadcast({"t": "usage_alert", "alert": self.usage_alert, "fresh": fresh})
                elif self.usage_alert:  # 窗口重置回落 / 用户调高了阈值
                    self.usage_alert = None
                    self.usage_note_sent.clear()
                    await self.broadcast({"t": "usage_alert", "alert": None})
            await asyncio.sleep(max(60, self.usage_poll_secs()))

    @staticmethod
    def usage_warn_pct():
        try:
            return int(db.get_meta("usage_warn_pct", config.USAGE_WARN_PCT))
        except (TypeError, ValueError):
            return config.USAGE_WARN_PCT

    @staticmethod
    def usage_poll_secs():
        try:
            return int(db.get_meta("usage_poll_secs", config.USAGE_POLL_SECONDS))
        except (TypeError, ValueError):
            return config.USAGE_POLL_SECONDS

    def usage_note(self, aid):
        """预警期间给正在干活的 agent 的收尾提醒（搭工具返回值，每个预警期每人只捎一次）。"""
        if not self.usage_alert or aid in self.usage_note_sent:
            return ""
        self.usage_note_sent.add(aid)
        return "\n\n" + self._usage_text()

    def _usage_text(self):
        a = self.usage_alert
        reset = a.get("resets_at")
        # 接口给的是 ISO 字符串（UTC）或数值时间戳，统一转本地时刻显示
        if isinstance(reset, str):
            try:
                from datetime import datetime
                reset = datetime.fromisoformat(reset).astimezone().timestamp()
            except ValueError:
                reset = None
        reset = f"，{time.strftime('%H:%M', time.localtime(reset))} 重置" if isinstance(reset, (int, float)) else ""
        return (f"【系统提醒】Claude 订阅 Session(5h) 用量已达 {a['pct']:.0f}%"
                f"（预警阈值 {a['threshold']}%{reset}）。请尽快把手头工作收到一个可交付的段落并汇报，"
                "不要开启新的大任务。")

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
            if self.auth_needed:
                continue  # 登录失效期间不唤醒（消息留在队列里，重新登录后一次补送）
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
        if self.usage_alert:  # 预警期间新唤醒的，开工前就知道要节制
            prompt += "\n\n" + self._usage_text()
        self.interrupted.discard(aid)

        cfg_path = self._write_mcp_config(agent)
        preset = config.PERMISSION_PRESETS.get(agent["permission"], config.PERMISSION_PRESETS["worker"])
        allowed = ",".join(config.ALLOWED_CHAT_TOOLS + preset["extra_allowed"])

        cmd = self._claude_cmd() + [
            # stream-json：逐事件输出，边跑边把"在想什么/用了什么工具"推给界面；
            # 最后一条 result 事件里带本次唤醒的 token 用量（-p 下 stream-json 必须配 --verbose）
            "-p", "--verbose", "--output-format", "stream-json",
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
        memories.ensure_claude_md(agent)
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
        self.activity[aid] = []
        result_evt = {}
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# cmd: {cmd}\n# ---- prompt ----\n{prompt}\n# ---- output ----\n")
            f.flush()
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=agent["cwd"], env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=f,
                limit=32 * 1024 * 1024,  # 单条事件可能很大（整段回复一行 JSON），别撞 StreamReader 默认 64K 上限
            )
            self.running[aid]["proc"] = proc
            self.running[aid]["log"] = log_path

            async def feed_stdin():
                try:
                    proc.stdin.write(prompt.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # 进程闪退时喂不进去，让读端收尾

            async def pump_stdout():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", "replace")
                    f.write(s)
                    f.flush()
                    await self._on_stream_line(aid, s, result_evt)
                await proc.wait()

            try:
                await asyncio.wait_for(asyncio.gather(feed_stdin(), pump_stdout()),
                                       timeout=config.WAKE_TIMEOUT)
                rc = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                rc = -1
                f.write("\n# !! 超时被杀\n")

        db.record_wake(aid)
        db.add_wake(aid, started, rc, self._usage_from(result_evt), log_path)
        stopped = self.running.get(aid, {}).get("stopped")
        interrupt = self.running.get(aid, {}).get("interrupt")

        # 先判"不是 agent 的锅"的失败：OAuth 过期 / 订阅用量打满（429）。
        # 这两种都不该记失败/自动暂停，而是全局挂起：消息排队不丢，
        # 顶栏提示用户（前者引导重新登录，后者显示重置时间等恢复后点重试）
        auth_err = limit_err = None
        if not stopped and not interrupt and (rc != 0 or result_evt.get("is_error")):
            tail = (str(result_evt.get("result") or "")) + "\n" + self._log_tail(log_path)
            auth_err = auth.find_auth_error(tail)
            if not auth_err:
                limit_err = auth.find_limit_error(tail)
                if not limit_err and result_evt.get("api_error_status") == 429:
                    limit_err = str(result_evt.get("result") or "").strip()[:300] or "usage limit (429)"

        if auth_err or limit_err:
            for cid, cur in prev.items():
                db.set_delivered(("agent", aid), cid, cur)  # 消息回队，恢复后重发
            if first and rc != 0:
                db.update_agent(aid, session_id=str(uuid.uuid4()))
            self.auth_needed = {"kind": "auth" if auth_err else "limit",
                                "agent": agent["name"], "detail": auth_err or limit_err, "ts": time.time()}
            await self.broadcast({"t": "auth", "needed": True, **self.auth_needed})
        elif rc == 0:
            self.fails[aid] = 0
            if self.auth_needed:  # 有唤醒成功了，说明登录已恢复
                self.auth_needed = None
                await self.broadcast({"t": "auth", "needed": False})
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

    # ---------- 过程动态（stream-json 事件 → 界面） ----------

    # 工具输入里最能说明"在干什么"的字段，按优先级取第一个有值的
    _DETAIL_KEYS = ("file_path", "path", "command", "pattern", "description",
                    "question", "url", "query", "prompt", "skill", "text")

    async def _on_stream_line(self, aid, line, result_evt):
        """解析 claude 输出的一行 stream-json 事件。
        assistant 事件里的 text（agent 的"自言自语"）和 tool_use（读文件/改文件/跑命令）
        转成过程动态推给界面；这些只进内存，绝不进聊天记录，不污染任何人的上下文。"""
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        etype = evt.get("type")
        if etype == "result":
            result_evt.update(evt)
            return
        if etype != "assistant":
            return
        for blk in (evt.get("message") or {}).get("content") or []:
            btype = blk.get("type")
            if btype == "text" and (blk.get("text") or "").strip():
                item = {"k": "note", "text": blk["text"].strip()[:240]}
            elif btype == "tool_use":
                item = {"k": "tool", "tool": blk.get("name") or "?",
                        "detail": self._tool_detail(blk.get("input") or {})}
            else:
                continue
            item["ts"] = time.time()
            acts = self.activity.setdefault(aid, [])
            acts.append(item)
            del acts[:-80]  # 只留最近 80 条
            await self.broadcast({"t": "act", "id": aid, "item": item})

    @classmethod
    def _tool_detail(cls, tool_input):
        for k in cls._DETAIL_KEYS:
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().replace("\n", " ")[:160]
        return ""

    # ---------- 工具 ----------

    @staticmethod
    def _log_tail(log_path, nbytes=8000):
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - nbytes))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    @staticmethod
    def _usage_from(result_evt):
        """从 stream-json 最后的 result 事件里抠 token 用量。缺了就算了，不影响主流程。"""
        if not result_evt:
            return None
        u = result_evt.get("usage") or {}
        return {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "cost_usd": result_evt.get("total_cost_usd", 0.0),
            "num_turns": result_evt.get("num_turns", 0),
        }

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
