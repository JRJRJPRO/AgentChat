"""订阅用量监控（UsageMixin，挂在 Hub 上）。

后台轮询 oauth/usage 元数据接口（零 token）：过阈值预警（给干活的 agent 捎收尾
提醒）、≥99% 主动全局挂起（agent 不去撞 429 → 不被误记失败）、窗口重置自动恢复；
接口被限流拿不到新数据时按钟表时间（resets_at）兜底撤警/解除。
状态都在 Hub 实例（self）上：usage_alert / usage_note_sent / usage_failing / auth_needed / auto_paused。
"""
import asyncio
import time

from . import config, db, usage


class UsageMixin:

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
                wp = self.usage_warn_pct()
                if pct >= wp and wp < 100:  # 阈值拉到 100 = 关闭预警
                    fresh = not self.usage_alert or self.usage_alert.get("resets_at") != row.get("resets_at")
                    if fresh:
                        self.usage_note_sent.clear()
                    self.usage_alert = {"pct": pct, "resets_at": row.get("resets_at"),
                                        "threshold": wp}
                    await self.broadcast({"t": "usage_alert", "alert": self.usage_alert, "fresh": fresh})
                elif self.usage_alert:  # 窗口重置回落 / 用户调高了阈值
                    self.usage_alert = None
                    self.usage_note_sent.clear()
                    await self.broadcast({"t": "usage_alert", "alert": None})
            # 预警还挂着但重置点已过（接口被限流拿不到新数据时会这样）→ 按钟表时间撤警，
            # 别拿上个窗口的旧数字一直喊"用量告急"（John 2026-08-02 实遇）
            if self.usage_alert and self._reset_passed(self.usage_alert.get("resets_at"), grace=60):
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
        if lim and self._reset_passed(lim.get("resets_at"), grace=600):
            return await self._lift_limit("Session 重置时间已过")  # 接口被限流查不到时也能按钟表恢复
        if lim and time.time() - lim["ts"] > 5 * 3600 + 600:
            await self._lift_limit("用量挂起已超过一个 Session 窗口，按时间兜底解除")

    @staticmethod
    def _reset_passed(iso, grace=0):
        """resets_at（ISO UTC 字符串）是否已过去 grace 秒以上。
        用量元数据接口被限流拿不到新数据时，靠钟表时间兜底判断窗口已重置。"""
        if not iso:
            return False
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return time.time() > ts.timestamp() + grace
        except ValueError:
            return False

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
