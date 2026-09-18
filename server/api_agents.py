"""agent 管理路由：创建 / 编辑 / 状态 / 停止与打断 / 压缩与上下文 / 私聊入口。"""
import os
import re

from fastapi import APIRouter, Body

from . import config, db, memories, skills
from .runtime import agent_view, err, hub, ws_manager

router = APIRouter()


def _clean_dirs(raw):
    return ",".join(d.strip() for d in (raw or "").replace("；", ",").replace("，", ",").split(",") if d.strip())


@router.post("/api/agents")
async def api_create_agent(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        err("名字不能为空")
    if db.get_agent_by_name(name):
        err(f"已存在同名 agent「{name}」")
    if name == config.USER_NAME or name.lower() == "user":
        err("这个名字保留给用户")
    cwd = (payload.get("cwd") or "").strip()
    if not cwd:
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)
        cwd = os.path.join(config.WORKSPACES_DIR, safe)
    model = payload.get("model") if payload.get("model") in config.MODELS else config.DEFAULT_MODEL
    perm = payload.get("permission") if payload.get("permission") in config.PERMISSION_PRESETS else config.DEFAULT_PERMISSION
    os.makedirs(cwd, exist_ok=True)
    a = db.create_agent(name, cwd, model, perm, payload.get("memo") or "")
    db.update_agent(a["id"], ask_perm=1 if payload.get("ask_perm") else 0)
    if payload.get("extra_dirs") is not None:
        db.update_agent(a["id"], extra_dirs=_clean_dirs(payload["extra_dirs"]))
    if payload.get("skills"):
        applied = skills.sync_agent_skills(a, payload["skills"])
        db.update_agent(a["id"], skills=",".join(applied))
    if payload.get("memories"):
        applied = memories.sync_agent_memories(a, payload["memories"])
        db.update_agent(a["id"], memories=",".join(applied))
    a = db.get_agent(a["id"])
    dm = db.ensure_dm(db.USER, ("agent", a["id"]))
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(a), "dm_conv_id": dm["id"]}


@router.post("/api/agents/{aid}/update")
async def api_update_agent(aid: int, payload: dict = Body(...)):
    a = db.get_agent(aid) or err("agent 不存在", 404)
    kw = {}
    if payload.get("model") in config.MODELS:
        kw["model"] = payload["model"]
    if payload.get("permission") in config.PERMISSION_PRESETS:
        kw["permission"] = payload["permission"]
    if "memo" in payload:
        kw["memo"] = payload["memo"] or ""
    if "email" in payload:
        kw["email"] = (payload["email"] or "").strip()[:200]
    if payload.get("cwd"):
        kw["cwd"] = payload["cwd"]
    if "extra_dirs" in payload:
        kw["extra_dirs"] = _clean_dirs(payload["extra_dirs"])
    if "ask_perm" in payload:
        kw["ask_perm"] = 1 if payload["ask_perm"] else 0
    if "skills" in payload:
        applied = skills.sync_agent_skills(a, payload["skills"] or [])
        kw["skills"] = ",".join(applied)
    if "memories" in payload:
        applied = memories.sync_agent_memories(a, payload["memories"] or [])
        kw["memories"] = ",".join(applied)
    db.update_agent(aid, **kw)
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(db.get_agent(aid))}


@router.post("/api/agents/{aid}/status")
async def api_agent_status(aid: int, payload: dict = Body(...)):
    db.get_agent(aid) or err("agent 不存在", 404)
    status = payload.get("status")
    if status not in ("active", "paused", "archived"):
        err("status 非法")
    db.update_agent(aid, status=status)
    if status == "active":
        hub.poke()  # 恢复后立刻补送积压消息
    await ws_manager.broadcast({"t": "agent", "id": aid, "run": hub.run_state(aid)})
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(db.get_agent(aid))}


@router.post("/api/agents/{aid}/stop")
async def api_agent_stop(aid: int):
    await hub.stop_agent(aid)
    return {"ok": True}


@router.post("/api/agents/{aid}/interrupt")
async def api_agent_interrupt(aid: int):
    ok = await hub.interrupt_agent(aid)
    return {"ok": ok}


@router.post("/api/agents/{aid}/compact")
async def api_agent_compact(aid: int):
    """开始压缩该 agent 的会话上下文（后台跑 /compact，立即返回）。
    进度经 ws 广播，结果在私聊留观察层记录。长上下文会让模型变笨且变贵。"""
    try:
        hub.start_compact(aid)
    except ValueError as e:
        err(str(e))
    return {"ok": True}


@router.post("/api/agents/{aid}/context")
async def api_agent_context(aid: int):
    """查该 agent 会话的上下文构成（无头跑 /context，零 API 调用，约 3-5 秒）。
    返回明细 markdown，顺手把 ctx 数字校准并广播。"""
    try:
        report = await hub.context_agent(aid)
    except ValueError as e:
        err(str(e))
    return {"ok": True, "report": report}


@router.get("/api/agents/{aid}/activity")
async def api_agent_activity(aid: int):
    """本轮唤醒的过程动态（思考/工具调用），刷新页面或中途打开会话时补拉用。"""
    return {"run": hub.run_state(aid), "items": hub.activity.get(aid, [])}


@router.post("/api/agents/{aid}/dm")
async def api_agent_dm(aid: int):
    db.get_agent(aid) or err("agent 不存在", 404)
    dm = db.ensure_dm(db.USER, ("agent", aid))
    return {"conv_id": dm["id"]}
