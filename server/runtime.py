"""进程内共享单例与路由公用小件。

路由模块（api_*.py）从这里取 hub / ws_manager / agent_view / err，
避免彼此 import 造成循环依赖：runtime 只依赖 hub.py，谁都可以依赖 runtime。
"""
import json

from fastapi import HTTPException, WebSocket

from .hub import Hub


class WSManager:
    def __init__(self):
        self.socks = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.socks.add(ws)

    def drop(self, ws):
        self.socks.discard(ws)

    async def broadcast(self, data: dict):
        text = json.dumps(data, ensure_ascii=False)
        for ws in list(self.socks):
            try:
                await ws.send_text(text)
            except Exception:
                self.socks.discard(ws)


ws_manager = WSManager()
hub = Hub(ws_manager.broadcast)


def err(msg, code=400):
    raise HTTPException(status_code=code, detail=msg)


def agent_view(a):
    """给前端的 agent 信息（去掉 token/session 等内部字段）。"""
    return {
        "id": a["id"], "name": a["name"], "cwd": a["cwd"], "model": a["model"],
        "permission": a["permission"], "memo": a["memo"], "status": a["status"],
        "wake_count": a["wake_count"], "last_wake_at": a["last_wake_at"],
        "extra_dirs": a.get("extra_dirs") or "", "skills": [s for s in (a.get("skills") or "").split(",") if s],
        "memories": [m for m in (a.get("memories") or "").split(",") if m],
        "ask_perm": bool(a.get("ask_perm")), "run": hub.run_state(a["id"]),
        "ctx_tokens": a.get("ctx_tokens") or 0, "ctx_window": a.get("ctx_window") or 0,
        "ctx_at": a.get("ctx_at") or 0, "email": a.get("email") or "",
    }
