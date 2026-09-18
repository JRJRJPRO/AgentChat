"""claude 子进程运行层（RunnerMixin，挂在 Hub 上）。

一次唤醒 = 一个 `claude -p --resume` 进程：命令拼装、env 注入（git-bash/第三方
provider）、stream-json 事件泵、静默超时看门、杀进程树（Windows 必须 taskkill /T）、
/compact 与 /context 的无头执行、唤醒日志的上下文回填。
状态都在 Hub 实例（self）上：running / activity / interrupted / fails / last_done。
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

from . import auth, config, db, memories, prompts, usage


class RunnerMixin:

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

    def _agent_env(self, agent=None):
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MCP_TOOL_TIMEOUT"] = "660000"  # ask_permission 要等用户，别被默认超时掐断
        if self.git_bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = self.git_bash
        # 第三方 Anthropic 兼容模型（data/providers.json）：注入端点+令牌，
        # 只作用于这个子进程，env 令牌优先于 claude.ai 登录态（实测）
        if agent:
            env.update(config.PROVIDER_ENV.get(agent["model"], {}))
        return env

    @staticmethod
    def _ctx_from(result_evt):
        """从 result 事件估算会话当前上下文长度：iterations[-1] 是本轮最后一次
        API 调用，它的 input+cache读+cache写 就是模型此刻看到的全部 token 数。"""
        u = (result_evt or {}).get("usage") or {}
        its = u.get("iterations") or []
        if not its:
            return None
        ctx = sum(its[-1].get(k, 0) for k in
                  ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        mu = result_evt.get("modelUsage") or {}
        win = max((v.get("contextWindow") or 0) for v in mu.values()) if mu else 0
        return (ctx, win) if ctx else None

    # ---------- /compact 与 /context（无头执行） ----------

    def start_compact(self, aid):
        """校验并启动一次后台 /compact，立即返回（不阻塞请求）。
        进度经 ws 广播 run="compacting"，结果（成功/失败）在私聊留观察层记录。
        各 agent 会话互不相干，可以同时压缩。"""
        agent = db.get_agent(aid)
        if not agent:
            raise ValueError("agent 不存在")
        if agent["status"] != "active" or not agent["bootstrapped"]:
            raise ValueError("只有已唤醒过的在岗 agent 才能压缩")
        info = self.running.get(aid)
        if info:
            raise ValueError("它已经在压缩了" if info.get("compact") else "它正在干活，等空闲后再压缩")
        self.running[aid] = {"proc": None, "since": time.time(),
                             "stopped": False, "compact": True}  # 占坑：压缩期间不被唤醒
        asyncio.create_task(self._compact_run(agent))

    async def _compact_run(self, agent):
        """对空闲 agent 的会话跑一次 /compact 压缩上下文。
        无头 -p --resume 模式实测有效（58.8k → 26.6k tokens，会话继续可用）。
        /compact 这一轮的 result 事件不报 usage，所以压缩完趁还占着坑
        （防止和新唤醒并发写会话文件）再跑一次 /context 把新长度当场量出来。"""
        aid = agent["id"]
        await self.broadcast({"t": "agent", "id": aid, "run": "compacting"})
        old = agent.get("ctx_tokens") or 0
        if old <= 0:  # 服务器重启后还没唤醒过：从最近一次唤醒日志回填压缩前长度
            old = ((await asyncio.to_thread(self._ctx_from_log, aid)) or (0, 0))[0]
        log_path = os.path.join(config.LOG_DIR, f"agent{aid}_compact_{int(time.time())}.log")
        err, new, win = None, None, 0
        try:
            cmd = self._claude_cmd() + [
                "-p", "--output-format", "json",
                "--model", config.MODEL_IDS.get(agent["model"], agent["model"]),
                "--resume", agent["session_id"],
            ]
            with open(log_path, "w", encoding="utf-8", errors="replace") as f:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=agent["cwd"], env=self._agent_env(agent),
                    stdin=asyncio.subprocess.PIPE, stdout=f, stderr=f)
                self.running[aid]["proc"] = proc
                try:
                    await asyncio.wait_for(proc.communicate(b"/compact"), timeout=600)
                except asyncio.TimeoutError:
                    self._kill_tree(proc)
                    err = "超时（10 分钟），已中止"
            if err is None and proc.returncode != 0:
                err = f"claude 退出码 {proc.returncode}，日志 {os.path.basename(log_path)}"
            if err is None:
                try:
                    _, new, win = await self._context_probe(agent)
                except Exception:
                    pass  # 量不出来就退回"待更新"，不算压缩失败
        except Exception as e:
            err = str(e)
        finally:
            self.running.pop(aid, None)
            await self.broadcast({"t": "agent", "id": aid, "run": "idle"})
            self.event.set()  # 压缩期间可能有消息排队
        if err is None:
            before = f"~{old // 1000}k" if old > 0 else "?"
            if new:
                db.update_agent(aid, ctx_tokens=new, ctx_window=win, ctx_at=time.time())
                await self.broadcast({"t": "ctx", "id": aid, "ctx": new, "win": win, "at": time.time()})
                text = f"已压缩「{agent['name']}」的上下文：{before} → {new // 1000}k tokens"
            else:
                db.update_agent(aid, ctx_tokens=-1, ctx_at=time.time())  # 探测没成，退回"待更新"
                await self.broadcast({"t": "ctx", "id": aid, "ctx": -1,
                                      "win": agent.get("ctx_window") or 0, "at": time.time()})
                text = f"已压缩「{agent['name']}」的上下文（压缩前 {before}，新长度下次唤醒后更新）"
        else:
            text = f"压缩「{agent['name']}」的上下文失败：{err}"
        dm = db.ensure_dm(db.USER, ("agent", aid))
        m = db.post_message(dm["id"], "note", 0, text, kind="compact")
        await self.broadcast({"t": "msg", "conv_id": dm["id"], "message": m})

    async def _context_probe(self, agent):
        """无头跑一次 /context，返回 (构成明细 markdown, 上下文 tokens, 窗口 tokens)。
        实测零 API 调用（duration_api_ms=0）约 3 秒；会往会话里追加 ~1k tokens 的
        本地命令记录，代价可忽略。调用方必须已占住 running 坑。"""
        cmd = self._claude_cmd() + [
            "-p", "--output-format", "json",
            "--model", config.MODEL_IDS.get(agent["model"], agent["model"]),
            "--resume", agent["session_id"],
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=agent["cwd"], env=self._agent_env(agent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        info = self.running.get(agent["id"])
        if info is not None:
            info["proc"] = proc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(b"/context"), timeout=120)
        except asyncio.TimeoutError:
            self._kill_tree(proc)
            raise ValueError("查询超时（2 分钟），已中止")
        if proc.returncode != 0:
            raise ValueError(f"claude 退出码 {proc.returncode}")
        evt = json.loads(out.decode("utf-8", errors="replace"))
        md = str(evt.get("result") or "")
        # 明细开头形如 "**Tokens:** 32.8k / 1m (3%)"
        m = re.search(r"\*\*Tokens:\*\*\s*([\d.]+)\s*([km]?)\s*/\s*([\d.]+)\s*([km]?)", md, re.I)
        if not (md and m):
            raise ValueError("没在输出里找到 Tokens 行（/context 格式可能变了）")
        unit = {"k": 1000, "m": 1000000, "": 1}
        ctx = int(float(m.group(1)) * unit[m.group(2).lower()])
        win = int(float(m.group(3)) * unit[m.group(4).lower()])
        return md, ctx, win

    async def context_agent(self, aid):
        """给界面用：占坑查一次上下文构成，返回明细 markdown（顺手把 ctx 数字校准）。"""
        agent = db.get_agent(aid)
        if not agent:
            raise ValueError("agent 不存在")
        if agent["status"] != "active" or not agent["bootstrapped"]:
            raise ValueError("只有已唤醒过的在岗 agent 才能查")
        info = self.running.get(aid)
        if info:
            raise ValueError("它正在压缩，稍等" if info.get("compact") else
                             "正在查了" if info.get("probe") else "它正在干活，等空闲后再查")
        self.running[aid] = {"proc": None, "since": time.time(),
                             "stopped": False, "probe": True}
        await self.broadcast({"t": "agent", "id": aid, "run": "probing"})
        try:
            md, ctx, win = await self._context_probe(agent)
            db.update_agent(aid, ctx_tokens=ctx, ctx_window=win, ctx_at=time.time())
            await self.broadcast({"t": "ctx", "id": aid, "ctx": ctx, "win": win, "at": time.time()})
            return md
        finally:
            self.running.pop(aid, None)
            await self.broadcast({"t": "agent", "id": aid, "run": "idle"})
            self.event.set()

    # ---------- 唤醒日志分析（重启后回填上下文数字） ----------

    def _scan_logs(self, aid):
        """列出该 agent 的唤醒日志（新→旧）和最近一次压缩日志的时间戳。"""
        wake_pat = re.compile(rf"^agent{aid}_(\d+)\.log$")
        comp_pat = re.compile(rf"^agent{aid}_compact_(\d+)\.log$")
        wakes, last_comp = [], 0
        try:
            for fn in os.listdir(config.LOG_DIR):
                if (m := wake_pat.match(fn)):
                    wakes.append((int(m.group(1)), fn))
                elif (m := comp_pat.match(fn)):
                    last_comp = max(last_comp, int(m.group(1)))
        except OSError:
            pass
        wakes.sort(reverse=True)
        return wakes, last_comp

    def _ctx_from_log(self, aid):
        """从该 agent 最近一次唤醒日志的 result 事件读上下文长度。
        服务器重启后 DB 里可能还是 0（迁移初值），日志里有真数。
        只信"最近一次压缩之后"的唤醒日志——压缩会改变上下文，更早的数字已失真。"""
        wakes, last_comp = self._scan_logs(aid)
        for ts, fn in wakes:
            if ts < last_comp:
                break  # 再往前都是压缩前的日志
            try:
                with open(os.path.join(config.LOG_DIR, fn), encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if '"type":"result"' not in line and '"type": "result"' not in line:
                    continue
                try:
                    ctxw = self._ctx_from(json.loads(line))
                except ValueError:
                    break
                if ctxw:
                    return ctxw
                break  # 这份日志的 result 没有 usage（异常轮），看更早一份
        return None

    def _backfill_ctx(self):
        """启动时给"还没统计过上下文"的 agent 从日志补一遍数，
        界面不用干等下次唤醒。ctx_tokens=-1（刚压缩过）不能用旧日志覆盖，跳过。"""
        for a in db.list_agents():
            if a["status"] == "archived" or not a.get("bootstrapped"):
                continue
            if (a.get("ctx_tokens") or 0) != 0:
                continue
            aid = a["id"]
            ctxw = self._ctx_from_log(aid)
            wakes, last_comp = self._scan_logs(aid)
            if ctxw:
                # 统计时刻 = 那次唤醒日志的时间戳（文件名里是开始时间，够近了）
                db.update_agent(aid, ctx_tokens=ctxw[0], ctx_window=ctxw[1],
                                ctx_at=wakes[0][0] if wakes else 0)
            elif last_comp and (not wakes or last_comp > wakes[0][0]):
                db.update_agent(aid, ctx_tokens=-1, ctx_at=last_comp)  # 压缩后还没醒过

    # ---------- 看门与杀进程 ----------

    async def _wake_watchdog(self, aid, proc, started):
        """活性看门：连续 WAKE_TIMEOUT 没有任何输出事件才算卡死（等待中——有挂起的
        工具调用，比如一条长 sleep——放宽一倍）；绝对上限 24h 防彻底失控。"""
        while proc.returncode is None:
            await asyncio.sleep(30)
            info = self.running.get(aid)
            if not info or info.get("proc") is not proc:
                return
            idle = time.time() - (info.get("last_evt") or started)
            allow = config.WAKE_TIMEOUT * (2 if info.get("tool_pending_ts") else 1)
            if idle > allow or time.time() - started > 24 * 3600:
                info["timed_out"] = True
                self._kill_tree(proc)
                return

    @staticmethod
    def _kill_tree(proc):
        """杀整棵进程树。Windows 上 claude 经 cmd 壳启动（.cmd 必须如此），
        proc.kill() 只杀得掉壳，里面的 node 孤儿会继续干活、stdout 也不到 EOF——
        这就是"点了打断/停止半天没反应"的根因。taskkill /T 连树带根一起清。"""
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15)
            else:
                proc.kill()
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            pass

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

        # 记忆自动挂载：agent 上轮自建的 memory/<包>/ 这轮就进 CLAUDE.md 导入块
        try:
            await asyncio.to_thread(memories.auto_mount, agent)
        except OSError:
            pass  # 挂载失败不挡唤醒

        # 推进派送游标（失败时回退）
        prev = {}
        for b in pend["batches"]:
            cid = b["conv"]["id"]
            prev[cid] = db.get_cursor(("agent", aid), cid)["last_delivered_id"]
            db.set_delivered(("agent", aid), cid, b["msgs"][-1]["id"])
            await self._receipt(aid, cid, b["msgs"][-1]["id"])  # ✓ 回执：消息进了唤醒词
        pend["prev_cursors"] = prev

        # 先广播"工作中"再插占位卡——顺序反了的话，直播卡到达前端时 S.working
        # 还没有这个 agent，会被渲染成空节点，之后怎么刷都找不回来（切走再切回才出现）
        self.activity[aid] = []
        self.running[aid]["shown"] = "working"
        self.running[aid]["last_evt"] = time.time()
        await self.broadcast({"t": "agent", "id": aid, "run": "working"})

        # 观察层：在每个触发本次唤醒的会话里插一条 note 占位（💭 过程卡）。
        # 只给用户看的时间线记录；干活途中有消息捎带送达时会"换段"（on_piggyback），
        # 让送达之后的思考排在新消息（和它的 ✓）下面；收工时各段分别定格。
        notes = {}
        for cid in prev:
            m = db.post_message(cid, "note", aid, "", kind="act")
            notes[cid] = {"mid": m["id"], "start": 0}
            await self.broadcast({"t": "msg", "conv_id": cid, "message": m})
        self.running[aid]["convs"] = list(prev.keys())
        self.running[aid]["notes"] = notes

        first = not agent["bootstrapped"]
        blocks = [prompts.batch_block(b["conv"], b["member_names"], b["msgs"]) for b in pend["batches"]]
        prompt = prompts.wake_prompt(agent, blocks, first, interrupted=aid in self.interrupted)
        if self.usage_alert:  # 预警期间新唤醒的，开工前就知道要节制；同时给用户留痕
            prompt += "\n\n" + self._usage_text()
            self.usage_note_sent.add(aid)  # 唤醒词里带过了，工具捎带就不重复提醒了
            await self._post_usage_note(agent["name"], list(prev.keys()))
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
        env = self._agent_env(agent)

        started = time.time()
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

            # 超时从"总时长一刀切"改成"静默超时"：agent 用 Claude 自带的等待
            # （长 sleep / 后台任务轮询）合法地跑几个小时也没事，只要还有动静；
            # 真卡死的进程不会再出任何事件，照样会被清理（John 拍板：信任 agent 的 loop）
            wd = asyncio.create_task(self._wake_watchdog(aid, proc, started))
            try:
                await asyncio.gather(feed_stdin(), pump_stdout())
                rc = proc.returncode
            finally:
                wd.cancel()
            if self.running.get(aid, {}).get("timed_out"):
                rc = -1
                f.write("\n# !! 静默超时被杀（太久没有任何输出事件）\n")

        # 观察层：把过程动态按段定格进各占位 note（换过段的只装自己那段；空段留空，前端不显示）
        items = self.activity.get(aid) or []
        for cid, n in (self.running.get(aid, {}).get("notes") or notes).items():
            seg = items[n["start"]:]
            m = db.update_message(n["mid"], json.dumps(seg, ensure_ascii=False) if seg else "")
            await self.broadcast({"t": "msg_update", "conv_id": cid, "message": m})

        db.record_wake(aid)
        db.add_wake(aid, started, rc, self._usage_from(result_evt), log_path)
        ctxw = self._ctx_from(result_evt)  # 会话当前上下文长度，给界面显示
        if ctxw:
            db.update_agent(aid, ctx_tokens=ctxw[0], ctx_window=ctxw[1], ctx_at=time.time())
            await self.broadcast({"t": "ctx", "id": aid, "ctx": ctxw[0], "win": ctxw[1],
                                  "at": time.time()})
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
                await self._receipt(aid, cid, cur)  # 回执收回：其实没送进去
            await self._revert_pig(aid, prev)
            if first and rc != 0:
                db.update_agent(aid, session_id=str(uuid.uuid4()))
            self.auth_needed = {"kind": "auth" if auth_err else "limit",
                                "agent": agent["name"], "detail": auth_err or limit_err, "ts": time.time()}
            if limit_err:  # 记下重置时间：用量监控靠它判断窗口何时重置好自动恢复
                try:
                    row = await asyncio.to_thread(usage.session_usage)
                    if row:
                        self.auth_needed["resets_at"] = row.get("resets_at")
                except Exception:
                    pass
            await self.broadcast({"t": "auth", "needed": True, **self.auth_needed})
        elif rc == 0:
            self.fails[aid] = 0
            if self.auth_needed:  # 有唤醒成功了，说明登录已恢复
                self.auth_needed = None
                await self.broadcast({"t": "auth", "needed": False})
            if first:
                db.update_agent(aid, bootstrapped=1)
            # 已读不回补救：干活途中捎带送达的消息，到收工都没在那个会话里回过一句
            # → 游标回退重新派送（下一轮是正常唤醒批而非捎带，不会无限循环）
            for cid, p in (self.running.get(aid, {}).get("pig") or {}).items():
                if not db.agent_replied_after(aid, cid, p["since"]):
                    db.set_delivered(("agent", aid), cid, p["prev"])
                    await self._receipt(aid, cid, p["prev"])
        elif stopped:
            # 用户硬停止：不算失败，也不重发（避免死循环）
            if first:
                db.update_agent(aid, session_id=str(uuid.uuid4()))  # 半截会话作废，防 session-id 冲突
        elif interrupt:
            # 软打断：回退游标让消息（连同打断者的新话）合并重发，标注"被打断"
            for cid, cur in prev.items():
                db.set_delivered(("agent", aid), cid, cur)
                await self._receipt(aid, cid, cur)
            await self._revert_pig(aid, prev)
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
                await self._receipt(aid, cid, cur)
            await self._revert_pig(aid, pend.get("prev_cursors") or {})
        if self.auth_needed:
            return  # 全局挂起期间的失败不是 agent 的锅（多半是撞上 429），不计数不暂停，恢复后重发
        self.fails[aid] = self.fails.get(aid, 0) + 1
        if self.fails[aid] >= config.MAX_CONSEC_FAILURES:
            db.update_agent(aid, status="paused")
            self.auto_paused[aid] = time.time()
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
        info = self.running.get(aid)
        if info:
            info["last_evt"] = time.time()  # 活性看门与"等待中"状态检测都靠它
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        etype = evt.get("type")
        if etype == "result":
            result_evt.update(evt)
            return
        if etype != "assistant":
            # 工具结果等事件回来了：不再处于"挂起的工具调用"（长 sleep 结束等）
            if info:
                info.pop("tool_pending_ts", None)
            return
        has_tool = False
        for blk in (evt.get("message") or {}).get("content") or []:
            btype = blk.get("type")
            if btype == "text" and (blk.get("text") or "").strip():
                item = {"k": "note", "text": blk["text"].strip()[:240]}
            elif btype == "tool_use":
                has_tool = True
                item = {"k": "tool", "tool": blk.get("name") or "?",
                        "detail": self._tool_detail(blk.get("input") or {})}
            else:
                continue
            item["ts"] = time.time()
            acts = self.activity.setdefault(aid, [])
            acts.append(item)
            del acts[:-80]  # 只留最近 80 条
            await self.broadcast({"t": "act", "id": aid, "item": item})
        if info:
            # 发起了工具调用 → 记挂起时刻（长 sleep/后台等待期间它一直挂着，
            # 超 2 分钟界面显示 ⏳ 等待中）；纯文字思考 → 清掉
            if has_tool:
                info["tool_pending_ts"] = time.time()
            else:
                info.pop("tool_pending_ts", None)

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
