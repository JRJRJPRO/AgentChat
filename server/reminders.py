"""⏰ 跨挂起定时提醒（RemindersMixin，挂在 Hub 上）。

agent 会话内的轮询活不过 -p 收工，所以闹钟由服务器保管（DB reminders 表），
到点发 kind="remind" 的系统消息唤醒；有检查命令的先零 token 跑通过才唤醒。
状态都在 Hub 实例（self）上：_rem_busy / _rem_last。
"""
import asyncio
import subprocess
import time

from . import db


class RemindersMixin:

    async def _check_reminders(self):
        """扫到点的 ⏰ 提醒（约 20 秒一次）。逐条起任务处理，检查命令不挡唤醒扫描。"""
        if time.time() - self._rem_last < 20:
            return
        self._rem_last = time.time()
        try:
            due = db.due_reminders(time.time())
        except Exception:
            return
        for r in due:
            if r["id"] not in self._rem_busy:
                self._rem_busy.add(r["id"])
                asyncio.create_task(self._fire_reminder(r))

    async def _fire_reminder(self, r):
        """到点的提醒：有检查命令先零 token 跑一下，成功才唤醒；没好就顺延重查。"""
        rid, aid = r["id"], r["agent_id"]
        try:
            agent = db.get_agent(aid)
            if not agent or agent["status"] == "archived":
                db.finish_reminder(rid)
                return
            tail = ""
            if r["check_cmd"]:
                out = await asyncio.to_thread(self._run_check, r["check_cmd"], agent["cwd"])
                if out is None:  # 条件还没满足
                    if time.time() < (r["expire_at"] or 0):
                        db.bump_reminder(rid, time.time() + max(60, r["recheck_secs"]))
                        return
                    tail = "\n（检查命令连续多日未成功，按超时唤醒你处理）"
                elif out:
                    tail = f"\n检查命令输出（尾部）：{out}"
            db.finish_reminder(rid)
            dm = db.ensure_dm(db.USER, ("agent", aid))
            m = db.post_message(dm["id"], "system", 0,
                                f"⏰ 你设的提醒到点了：{r['note']}{tail}", kind="remind")
            await self.broadcast({"t": "msg", "conv_id": dm["id"], "message": m})
            self.poke()
        finally:
            self._rem_busy.discard(rid)

    @staticmethod
    def _run_check(cmd, cwd):
        """跑提醒的检查命令：退出码 0 → 返回输出尾部（可能为空串），非 0/异常 → None。"""
        try:
            p = subprocess.run(cmd, shell=True, cwd=cwd or None,
                               capture_output=True, timeout=120)
            if p.returncode != 0:
                return None
            return (p.stdout or b"").decode("utf-8", "replace").strip()[-400:]
        except Exception:
            return None
