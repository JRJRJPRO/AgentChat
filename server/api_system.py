"""系统路由：页面状态 / 用量与预警设置 / Claude 登录 / 技能记忆清单 / 统计 / 关机。"""
import os
import time

from fastapi import APIRouter, Body

from . import auth, config, db, memories, skills, usage
from .runtime import agent_view, err, hub

router = APIRouter()


@router.get("/api/state")
async def api_state():
    return {
        "user_name": config.USER_NAME,
        "agents": [agent_view(a) for a in db.list_agents()],
        "convs": db.user_conversations(),
        "models": config.MODELS,
        "permissions": list(config.PERMISSION_PRESETS.keys()),
        "defaults": {"model": config.DEFAULT_MODEL, "permission": config.DEFAULT_PERMISSION,
                     "chain_limit": config.DEFAULT_CHAIN_LIMIT,
                     "paste_doc_threshold": config.PASTE_DOC_THRESHOLD},
        "auth": hub.auth_needed,
        "usage_monitor": {"warn_pct": hub.usage_warn_pct(), "poll_secs": hub.usage_poll_secs(),
                          "alert": hub.usage_alert, "failing": bool(hub.usage_failing),
                          "fail_reason": hub.usage_failing if isinstance(hub.usage_failing, str) else
                                         ("error" if hub.usage_failing else None),
                          "retry_at": usage.status()["retry_at"]},
    }


@router.post("/api/usage_settings")
async def api_usage_settings(payload: dict = Body(...)):
    """设置里的两个滑杆：预警阈值 / 轮询间隔。存 db.meta，监控循环每轮现读、立即生效。"""
    if "warn_pct" in payload:
        db.set_meta("usage_warn_pct", max(30, min(100, int(payload["warn_pct"]))))  # 100 = 关闭预警
    if "poll_secs" in payload:
        db.set_meta("usage_poll_secs", max(60, min(900, int(payload["poll_secs"]))))
    return {"warn_pct": hub.usage_warn_pct(), "poll_secs": hub.usage_poll_secs()}


# ---------------- Claude 登录（OAuth 过期修复） ----------------

@router.get("/api/auth/status")
async def api_auth_status():
    return {"needed": hub.auth_needed, "credentials": auth.credentials_status()}


@router.post("/api/auth/login")
async def api_auth_login():
    """开一个终端窗口跑 claude /login（自动跳浏览器完成 OAuth）。"""
    try:
        auth.launch_login(hub.claude)
    except Exception as e:
        err(f"打开登录窗口失败: {e}", 500)
    return {"ok": True}


@router.post("/api/auth/clear")
async def api_auth_clear():
    """用户确认已重新登录：解除封锁，积压消息立刻补送。"""
    await hub.clear_auth()
    return {"ok": True}


# ---------------- 技能 / 记忆清单（弹窗勾选列表用） ----------------

@router.get("/api/skills")
async def api_skills():
    return {
        "library": skills.list_library(),
        "global": skills.list_global(),
        "library_dir": config.SKILLS_DIR,
        "global_dir": config.GLOBAL_SKILLS_DIR,
    }


@router.get("/api/memories")
async def api_memories():
    return {"library": memories.list_library(), "dir": config.MEMORIES_DIR}


@router.post("/api/open_folder")
async def api_open_folder(payload: dict = Body(...)):
    # 只允许打开这几个资源目录，别的路径不开（本地工具，防手滑）
    path = payload.get("path") or ""
    if path not in (config.SKILLS_DIR, config.GLOBAL_SKILLS_DIR, config.MEMORIES_DIR):
        err("只允许打开技能/记忆目录")
    os.makedirs(path, exist_ok=True)
    os.startfile(path)
    return {"ok": True}


# ---------------- 统计与关机 ----------------

@router.get("/api/stats")
async def api_stats(hours: float = 5):
    return db.usage_stats(time.time() - hours * 3600)


@router.get("/api/usage")
def api_usage():  # 同步 def：订阅接口是阻塞网络调用，FastAPI 会丢线程池跑
    return {
        "subscription": usage.subscription_usage(),
        "local": db.usage_stats(time.time() - 5 * 3600),
    }


@router.post("/api/shutdown")
async def api_shutdown():
    import asyncio
    asyncio.get_event_loop().call_later(0.3, os._exit, 0)
    return {"ok": True}
