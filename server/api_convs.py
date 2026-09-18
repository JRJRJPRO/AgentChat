"""会话路由：列表 / 建群 / 消息 / 发送 / 附件上传 / 已读 / 成员 / 设置 / 链长重置。"""
import base64
import os
import re
import time

from fastapi import APIRouter, Body

from . import config, db
from .runtime import err, hub, ws_manager

router = APIRouter()


@router.get("/api/convs")
async def api_convs(scope: str = "mine"):
    return {"convs": db.all_conversations() if scope == "all" else db.user_conversations()}


@router.post("/api/convs")
async def api_create_conv(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip() or "新群聊"
    members = [("agent", int(x)) for x in payload.get("agent_ids") or []]
    if payload.get("include_user", True):
        members.insert(0, db.USER)
    if len(members) < 2:
        err("群聊至少需要两个成员")
    cid = db.create_conversation("group", name, "user", members)
    msg = db.post_message(cid, "system", 0, f"{config.USER_NAME} 创建了群聊「{name}」", kind="sys")
    await ws_manager.broadcast({"t": "convs_changed"})
    await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    return {"conv_id": cid}


@router.get("/api/convs/{cid}")
async def api_conv_detail(cid: int):
    c = db.get_conv(cid) or err("会话不存在", 404)
    return {"conv": db.conv_summary(c)}


@router.get("/api/convs/{cid}/messages")
async def api_messages(cid: int, before_id: int = 0, limit: int = 50):
    db.get_conv(cid) or err("会话不存在", 404)
    msgs, has_more = db.list_messages(cid, before_id or None, min(limit, 200))
    return {"messages": msgs, "has_more": has_more}


@router.post("/api/convs/{cid}/send")
async def api_send(cid: int, payload: dict = Body(...)):
    db.get_conv(cid) or err("会话不存在", 404)
    if not db.is_member(cid, "user", 0):
        err("你不在这个会话里，先加入才能发言")
    text = (payload.get("text") or "").strip()
    atts = _clean_attachments(payload.get("attachments"))
    if not text and not atts:
        err("消息不能为空")
    msg = db.post_message(cid, "user", 0, text, attachments=atts)
    await hub.chain_clear(cid)  # 用户发言会重置链长
    await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    hub.poke()
    return {"message": msg}


# ---------------- 聊天附件（拖拽图片 / 大段文本转临时文档） ----------------

def _clean_attachments(raw):
    """只收之前经 /upload 落盘、路径确实在 uploads 目录里的附件，防伪造路径。"""
    out = []
    root = os.path.abspath(config.UPLOADS_DIR) + os.sep
    for a in (raw or [])[:9]:
        if not isinstance(a, dict):
            continue
        p = os.path.abspath(a.get("path") or "")
        if p.startswith(root) and os.path.isfile(p):
            out.append({"kind": a.get("kind") or "file", "name": str(a.get("name") or "")[:120],
                        "path": p, "url": str(a.get("url") or ""), "size": int(a.get("size") or 0)})
    return out


_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


@router.post("/api/convs/{cid}/upload")
async def api_upload(cid: int, payload: dict = Body(...)):
    """收一个附件：{name, data(base64)} 是二进制文件（拖拽/粘贴的图片等）；
    {name?, text} 是大段文本，落成 .md"临时文档"。返回附件描述，随消息一起发送。"""
    db.get_conv(cid) or err("会话不存在", 404)
    if not db.is_member(cid, "user", 0):
        err("你不在这个会话里")
    name = re.sub(r'[\\/:*?"<>|\r\n]', "_", (payload.get("name") or "").strip()) or "附件"
    if payload.get("text") is not None:
        if not name.lower().endswith((".md", ".txt")):
            name += ".md"
        data = payload["text"].encode("utf-8")
        kind = "text"
    else:
        try:
            data = base64.b64decode(payload.get("data") or "", validate=True)
        except Exception:
            err("data 不是合法的 base64")
        kind = "image" if os.path.splitext(name)[1].lower() in _IMG_EXTS else "file"
    if not data:
        err("附件是空的")
    if len(data) > config.UPLOAD_MAX_BYTES:
        err(f"附件太大（上限 {config.UPLOAD_MAX_BYTES // 1024 // 1024}MB）")
    subdir = os.path.join(config.UPLOADS_DIR, f"conv{cid}")
    os.makedirs(subdir, exist_ok=True)
    fname = f"{int(time.time() * 1000)}_{name}"
    fpath = os.path.join(subdir, fname)
    with open(fpath, "wb") as f:
        f.write(data)
    return {"attachment": {"kind": kind, "name": name, "path": fpath,
                           "url": f"/uploads/conv{cid}/{fname}", "size": len(data)}}


@router.post("/api/convs/{cid}/read")
async def api_read(cid: int, payload: dict = Body(...)):
    db.mark_read(db.USER, cid, int(payload.get("last_id") or 0))
    await ws_manager.broadcast({"t": "read", "conv_id": cid})
    return {"ok": True}


@router.post("/api/convs/{cid}/members")
async def api_members(cid: int, payload: dict = Body(...)):
    c = db.get_conv(cid) or err("会话不存在", 404)
    if c["type"] == "dm":
        err("私聊不能改成员")
    events = []
    for aid in payload.get("add_agent_ids") or []:
        a = db.get_agent(int(aid))
        if a and not db.is_member(cid, "agent", a["id"]):
            db.add_member(cid, "agent", a["id"])
            events.append(f"{config.USER_NAME} 邀请「{a['name']}」加入了群聊")
    for aid in payload.get("remove_agent_ids") or []:
        a = db.get_agent(int(aid))
        if a and db.is_member(cid, "agent", a["id"]):
            db.remove_member(cid, "agent", a["id"])
            events.append(f"{config.USER_NAME} 将「{a['name']}」移出了群聊")
    if payload.get("join_user") and not db.is_member(cid, "user", 0):
        db.add_member(cid, "user", 0)
        events.append(f"{config.USER_NAME} 加入了群聊")
    if payload.get("leave_user") and db.is_member(cid, "user", 0):
        db.remove_member(cid, "user", 0)
        events.append(f"{config.USER_NAME} 退出了群聊")
    for e in events:
        msg = db.post_message(cid, "system", 0, e, kind="sys")
        await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"conv": db.conv_summary(db.get_conv(cid))}


@router.post("/api/convs/{cid}/settings")
async def api_conv_settings(cid: int, payload: dict = Body(...)):
    c = db.get_conv(cid) or err("会话不存在", 404)
    if "name" in payload and c["type"] == "group":
        db.update_conv(cid, name=(payload["name"] or "").strip() or c["name"])
    if "chain_limit" in payload:
        v = payload["chain_limit"]
        db.update_conv(cid, chain_limit=int(v) if v else None)
        hub.poke()
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"conv": db.conv_summary(db.get_conv(cid))}


@router.post("/api/convs/{cid}/chain_reset")
async def api_chain_reset(cid: int):
    db.get_conv(cid) or err("会话不存在", 404)
    msg = db.post_message(cid, "system", 0, f"{config.USER_NAME} 允许 agent 继续对话", kind="chain_reset")
    await hub.chain_clear(cid)
    await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    hub.poke()
    return {"ok": True}
