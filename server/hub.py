"""Hub 调度器：AgentChat 的心脏（调度决策层）。

工作方式（解决"agent 立即收到消息但等待时不烧 token"）：
- agent 平时不运行。每个 agent 对应一个持久的 claude 会话 id。
- 有新消息 → poke() 唤醒扫描循环 → 攒批(debounce) → 对每个有未派送消息的
  agent 启动一次 `claude -p --resume <会话id>`，把新消息喂进去。
- agent 用 chat MCP 工具回复/建群，回合结束进程退出，继续挂起。
- 期间来的新消息排队，本轮结束后再触发下一轮唤醒。

失败处理：唤醒失败回退派送游标（消息会重发），连续失败自动暂停该 agent
并在与用户的私聊里留言说明。

v4.0 起按域拆分（状态全在本类实例上，方法通过 mixin 挂载）：
  runner.py    —— claude 子进程运行层（唤醒/压缩/探测/杀树/日志分析）
  usagemon.py  —— 订阅用量监控与全局挂起
  reminders.py —— ⏰ 跨挂起定时提醒
本文件只保留：状态定义、运行状态机、打断/停止入口、派送扫描主循环、
捎带/回执/链长等消息簿记钩子。
"""
import asyncio
import json
import shutil
import time

from . import config, db
from .reminders import RemindersMixin
from .runner import RunnerMixin
from .usagemon import UsageMixin


class Hub(RunnerMixin, UsageMixin, RemindersMixin):
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
        self._rem_busy = set()   # 正在处理（跑检查命令）的提醒 id，防重复触发
        self._rem_last = 0.0     # 上次扫描提醒的时刻（节流）
        self.claude = shutil.which("claude")
        self.git_bash = self._find_git_bash()  # Windows 上 claude 需要 git-bash

    # ---------- 运行状态机 ----------
    # 运行态（内存）：idle | working | waiting | compacting | probing
    # 持久态（DB status）：active | paused | archived（v4.1 起归档改分组、新增 held）
    # 全局态（独立轴）：auth_needed = None | auth | limit，置位期间压所有唤醒

    def poke(self):
        self.event.set()

    def run_state(self, aid):
        info = self.running.get(aid)
        if not info:
            return "idle"
        if info.get("compact"):
            return "compacting"
        if info.get("probe"):
            return "probing"
        return "waiting" if self._is_waiting(info) else "working"

    @staticmethod
    def _is_waiting(info):
        """"等待中"：一个工具调用挂起超过 2 分钟没回来——多半是长 sleep、
        等实验/后台任务（Claude 自带的等待方式，John 拍板允许并单独显示）。"""
        ts = info.get("tool_pending_ts")
        return bool(ts) and time.time() - ts > 120

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

    # ---------- 消息簿记钩子（回执 / 捎带 / 链长） ----------

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

    async def _notify_chain(self, cid):
        if cid not in self.chain_notified:
            self.chain_notified.add(cid)
            await self.broadcast({"t": "chain", "conv_id": cid, "paused": True})

    async def clear_auth(self):
        """用户在界面上点了"已登录，重试/重试"：解除唤醒封锁并立刻补送积压消息。"""
        if (self.auth_needed or {}).get("kind") == "limit":
            return await self._lift_limit("用量挂起已手动解除")  # 连带平反误暂停的 agent
        if self.auth_needed:
            self.auth_needed = None
            await self.broadcast({"t": "auth", "needed": False})
        self.fails.clear()
        self.poke()

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
            await self._check_reminders()  # ⏰ 到点的提醒（发消息不受挂起影响，唤醒会排队）
            # 工作中 ⇄ 等待中 的状态翻转推给界面（主循环最迟 5s 一拍，够顺滑）
            for aid2, info in list(self.running.items()):
                if info.get("compact") or info.get("probe") or not info.get("proc"):
                    continue
                st = "waiting" if self._is_waiting(info) else "working"
                if info.get("shown") != st:
                    info["shown"] = st
                    await self.broadcast({"t": "agent", "id": aid2, "run": st})
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

