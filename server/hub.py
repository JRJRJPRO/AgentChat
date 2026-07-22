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
import re
import shutil
import subprocess
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
        self.auth_needed = None  # 登录失效/用量打满时 {"kind","agent","detail","ts",("resets_at")}；置位期间暂停一切唤醒
        self.auto_paused = {}    # agent_id -> 被"连续失败"自动暂停的时刻；用量窗口重置时按时间窗自动平反
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
        info = self.running.get(aid)
        if not info:
            return "idle"
        if info.get("compact"):
            return "compacting"
        return "probing" if info.get("probe") else "working"

    async def stop_agent(self, aid):
        """硬停止：杀进程树，这批消息不再重发。"""
        info = self.running.get(aid)
        if info and info.get("proc"):
            info["stopped"] = True
            self._kill_tree(info["proc"])

    async def interrupt_agent(self, aid):
        """软打断：杀进程树，但消息回退重发，下次唤醒带"你被打断了"标注，
        agent 借 --resume 的记忆接着干，不从头重来。"""
        info = self.running.get(aid)
        if not (info and info.get("proc")):
            return False
        info["interrupt"] = True
        self._kill_tree(info["proc"])
        return True

    def _agent_env(self):
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MCP_TOOL_TIMEOUT"] = "660000"  # ask_permission 要等用户，别被默认超时掐断
        if self.git_bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = self.git_bash
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
                    *cmd, cwd=agent["cwd"], env=self._agent_env(),
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
            *cmd, cwd=agent["cwd"], env=self._agent_env(),
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

    async def _receipt(self, aid, cid, upto):
        """✓ 已送达回执：该 agent 在这个会话里的投递游标动了（推进=收到，回退=收回）。"""
        await self.broadcast({"t": "receipt", "conv_id": cid, "agent_id": aid, "upto": upto})

    async def _revert_pig(self, aid, already):
        """唤醒失败/被打断时，把干活途中捎带送达的消息也回队（以前会悄悄丢掉）。
        already 里已回退到更早游标的会话跳过，别用较新的捎带游标盖回去。"""
        for cid, p in (self.running.get(aid, {}).get("pig") or {}).items():
            if cid not in already:
                db.set_delivered(("agent", aid), cid, p["prev"])
                await self._receipt(aid, cid, p["prev"])

    async def on_piggyback(self, aid, cid, prev_cur, upto):
        """干活途中有消息捎带送达该会话时调用：
        ①过程卡"换段"——旧段就地定格，新开一张占位卡排在新消息（和 ✓）下面，
          之后的思考自然显示在新消息之后，时间线顺序不再错乱；
        ②记下送达前的游标，收工时若"已读不回"则回退重派（见 _wake 成功分支）。"""
        info = self.running.get(aid)
        if not info:
            return
        info.setdefault("pig", {}).setdefault(cid, {"prev": prev_cur, "since": upto})
        items = self.activity.get(aid) or []
        notes = info.setdefault("notes", {})
        n = notes.get(cid)
        if n is not None:  # 旧段定格
            seg = items[n["start"]:]
            m = db.update_message(n["mid"], json.dumps(seg, ensure_ascii=False) if seg else "")
            await self.broadcast({"t": "msg_update", "conv_id": cid, "message": m})
        m2 = db.post_message(cid, "note", aid, "", kind="act")
        notes[cid] = {"mid": m2["id"], "start": len(items)}
        if cid not in (info.get("convs") or []):
            info.setdefault("convs", []).append(cid)  # 用量提醒留痕等也认这个会话了
        await self.broadcast({"t": "msg", "conv_id": cid, "message": m2})

    async def chain_clear(self, cid):
        if cid in self.chain_notified:
            self.chain_notified.discard(cid)
            await self.broadcast({"t": "chain", "conv_id": cid, "paused": False})

    async def clear_auth(self):
        """用户在界面上点了"已登录，重试/重试"：解除唤醒封锁并立刻补送积压消息。"""
        if (self.auth_needed or {}).get("kind") == "limit":
            return await self._lift_limit("用量挂起已手动解除")  # 连带平反误暂停的 agent
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
            st = usage.status()
            data_fresh = row is not None and time.time() - st["data_ts"] < 300  # 退避期的旧数据不拿来做判定
            if st["kind"] == "rate_limited" and not data_fresh:
                # 元数据接口自己被限流（2026-07-21 实见 429 rate_limit_error）：
                # 不是格式坏了，退避重试就行；界面如实显示"限流中，几点重试"
                if self.usage_failing != "rate_limited":
                    self.usage_failing = "rate_limited"
                    await self.broadcast({"t": "usage_fail", "failing": True,
                                          "reason": "rate_limited", "retry_at": st["retry_at"]})
            elif row is None:
                fails += 1
                if fails >= 3 and not self.usage_failing:
                    self.usage_failing = True
                    await self.broadcast({"t": "usage_fail", "failing": True, "reason": "error"})
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
            await self._limit_watch(row if data_fresh else None)
            # 用量挂起期间盯得勤一点（最多 2 分钟一查），重置后能尽快自动恢复
            secs = max(60, self.usage_poll_secs())
            if (self.auth_needed or {}).get("kind") == "limit":
                secs = min(secs, 120)
            await asyncio.sleep(secs)

    async def _limit_watch(self, row):
        """用量打满的自动挂起与自动恢复（John：满了之后不该还要手动恢复）。
        - 用量 ≥99% → 主动全局挂起：agent 根本不会去撞 429，也就不会被记失败误暂停；
        - 挂起中检测到窗口重置（resets_at 变了 / 用量回落）→ 自动解除并补送积压消息；
        - 接口查不到时按时间兜底：挂起超过一个 Session 窗口（5h）自动解除。"""
        lim = self.auth_needed if (self.auth_needed or {}).get("kind") == "limit" else None
        if row is not None:
            pct = row.get("utilization") or 0
            if pct >= 99 and not self.auth_needed:
                self.auth_needed = {"kind": "limit", "agent": "",
                                    "detail": f"Session(5h) 用量已达 {pct:.0f}%",
                                    "ts": time.time(), "resets_at": row.get("resets_at")}
                await self.broadcast({"t": "auth", "needed": True, **self.auth_needed})
                return
            if lim:
                if not lim.get("resets_at"):
                    lim["resets_at"] = row.get("resets_at")  # 唤醒失败路径挂起时没拿到，补上
                elif row.get("resets_at") and row["resets_at"] != lim["resets_at"]:
                    return await self._lift_limit("Session 用量窗口已重置")
                if pct < 90:
                    return await self._lift_limit("Session 用量已回落")
        if lim and time.time() - lim["ts"] > 5 * 3600 + 600:
            await self._lift_limit("用量挂起已超过一个 Session 窗口，按时间兜底解除")

    async def _lift_limit(self, reason):
        """解除用量挂起：清失败计数、平反挂起前后被误暂停的 agent、补送积压消息。"""
        start = (self.auth_needed or {}).get("ts") or time.time()
        self.auth_needed = None
        self.fails.clear()
        await self.broadcast({"t": "auth", "needed": False})
        revived = False
        for aid, ts in list(self.auto_paused.items()):
            # 挂起前 15 分钟内的自动暂停大概率也是 429 的误伤（比如两次轮询间隙撞上的）
            if ts >= start - 900:
                agent = db.get_agent(aid)
                if agent and agent["status"] == "paused":
                    db.update_agent(aid, status="active")
                    revived = True
                    dm = db.ensure_dm(db.USER, ("agent", aid))
                    m = db.post_message(dm["id"], "system", 0,
                                        f"{reason}，已自动恢复「{agent['name']}」，积压消息将补送。")
                    await self.broadcast({"t": "msg", "conv_id": dm["id"], "message": m})
            self.auto_paused.pop(aid, None)
        if revived:
            await self.broadcast({"t": "convs_changed"})
        self.poke()  # 挂起期间排队的消息立刻补送

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

    async def usage_note(self, aid):
        """预警期间给正在干活的 agent 的收尾提醒（搭工具返回值，每个预警期每人只捎一次）。
        同时在该 agent 本轮的会话里留一条 ⚠ note，让用户看到"系统提醒过它了"。"""
        if not self.usage_alert or aid in self.usage_note_sent:
            return ""
        self.usage_note_sent.add(aid)
        info = self.running.get(aid) or {}
        agent = db.get_agent(aid)
        await self._post_usage_note(agent["name"] if agent else f"agent#{aid}", info.get("convs") or [])
        return "\n\n" + self._usage_text()

    async def _post_usage_note(self, name, cids):
        """观察层留痕：⚠ 系统已提醒某 agent 尽快收尾（只用户可见，永久保留）。"""
        text = f"系统已提醒「{name}」：" + self._usage_text().replace("【系统提醒】", "")
        for cid in cids:
            m = db.post_message(cid, "note", 0, text, kind="usage")
            await self.broadcast({"t": "msg", "conv_id": cid, "message": m})

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
        await asyncio.to_thread(self._backfill_ctx)  # 重启后先把各 agent 的上下文数补出来
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
        env = self._agent_env()

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

            try:
                await asyncio.wait_for(asyncio.gather(feed_stdin(), pump_stdout()),
                                       timeout=config.WAKE_TIMEOUT)
                rc = proc.returncode
            except asyncio.TimeoutError:
                self._kill_tree(proc)
                await proc.wait()
                rc = -1
                f.write("\n# !! 超时被杀\n")

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
