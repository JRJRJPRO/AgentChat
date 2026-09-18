"""AgentChat 服务器装配层。

启动：python -m uvicorn server.main:app --port 8787

v4.0 起本文件只做装配：FastAPI app、生命周期、中间件、路由挂载、WebSocket、静态文件。
路由实现按域拆在 api_*.py，进程内单例（hub / ws_manager / agent_view / err）在 runtime.py，
调度器本体在 hub.py（runner / usagemon / reminders 三个 mixin 分文件）。
WS 事件契约见 docs/ws-events.md。
"""
import asyncio
import contextlib
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import api_agents, api_convs, api_library, api_system, api_tools, config, db
from .runtime import hub, ws_manager


@contextlib.asynccontextmanager
async def lifespan(app):
    config.ensure_dirs()
    db.init()
    task = asyncio.create_task(hub.run())
    mon = asyncio.create_task(hub.usage_monitor())
    yield
    task.cancel()
    mon.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """本地服务器，静态文件禁缓存：防止浏览器拿旧 css/js 和新版本混搭出怪样。
    no-cache = 每次向服务器确认（未变返回 304），文件都在本机，零成本。"""
    resp = await call_next(request)
    p = request.url.path
    if not (p.startswith("/api") or p.startswith("/internal") or p == "/ws"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


for _r in (api_agents, api_convs, api_library, api_system, api_tools):
    app.include_router(_r.router)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # 客户端 ping，忽略内容
    except WebSocketDisconnect:
        ws_manager.drop(ws)
    except Exception:
        ws_manager.drop(ws)


# 静态文件放最后挂载，让 /api /ws 优先匹配
os.makedirs(config.UPLOADS_DIR, exist_ok=True)  # mount 时目录必须已存在（lifespan 还没跑）
app.mount("/uploads", StaticFiles(directory=config.UPLOADS_DIR), name="uploads")
app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
