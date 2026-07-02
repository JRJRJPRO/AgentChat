"""AgentChat 服务器：Web UI 接口 + agent 工具接口 + WebSocket 实时推送。

启动：python -m uvicorn server.main:app --port 8787
"""
import contextlib
import json
import os
import re
import time

from fastapi import FastAPI, Body, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, prompts, skills, usage
from .hub import Hub


# ---------------- WebSocket 管理 ----------------

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


@contextlib.asynccontextmanager
async def lifespan(app):
    config.ensure_dirs()
    db.init()
    import asyncio
    task = asyncio.create_task(hub.run())
    yield
    task.cancel()


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


def agent_view(a):
    """给前端的 agent 信息（去掉 token/session 等内部字段）。"""
    return {
        "id": a["id"], "name": a["name"], "cwd": a["cwd"], "model": a["model"],
        "permission": a["permission"], "memo": a["memo"], "status": a["status"],
        "wake_count": a["wake_count"], "last_wake_at": a["last_wake_at"],
        "extra_dirs": a.get("extra_dirs") or "", "skills": [s for s in (a.get("skills") or "").split(",") if s],
        "ask_perm": bool(a.get("ask_perm")), "run": hub.run_state(a["id"]),
    }


def err(msg, code=400):
    raise HTTPException(status_code=code, detail=msg)


# ---------------- 页面状态 ----------------

@app.get("/api/state")
async def api_state():
    return {
        "user_name": config.USER_NAME,
        "agents": [agent_view(a) for a in db.list_agents()],
        "convs": db.user_conversations(),
        "models": config.MODELS,
        "permissions": list(config.PERMISSION_PRESETS.keys()),
        "defaults": {"model": config.DEFAULT_MODEL, "permission": config.DEFAULT_PERMISSION,
                     "chain_limit": config.DEFAULT_CHAIN_LIMIT},
    }


# ---------------- agent 管理 ----------------

@app.post("/api/agents")
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
    a = db.get_agent(a["id"])
    dm = db.ensure_dm(db.USER, ("agent", a["id"]))
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(a), "dm_conv_id": dm["id"]}


def _clean_dirs(raw):
    return ",".join(d.strip() for d in (raw or "").replace("；", ",").replace("，", ",").split(",") if d.strip())


@app.post("/api/agents/{aid}/update")
async def api_update_agent(aid: int, payload: dict = Body(...)):
    a = db.get_agent(aid) or err("agent 不存在", 404)
    kw = {}
    if payload.get("model") in config.MODELS:
        kw["model"] = payload["model"]
    if payload.get("permission") in config.PERMISSION_PRESETS:
        kw["permission"] = payload["permission"]
    if "memo" in payload:
        kw["memo"] = payload["memo"] or ""
    if payload.get("cwd"):
        kw["cwd"] = payload["cwd"]
    if "extra_dirs" in payload:
        kw["extra_dirs"] = _clean_dirs(payload["extra_dirs"])
    if "ask_perm" in payload:
        kw["ask_perm"] = 1 if payload["ask_perm"] else 0
    if "skills" in payload:
        applied = skills.sync_agent_skills(a, payload["skills"] or [])
        kw["skills"] = ",".join(applied)
    db.update_agent(aid, **kw)
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(db.get_agent(aid))}


@app.post("/api/agents/{aid}/status")
async def api_agent_status(aid: int, payload: dict = Body(...)):
    a = db.get_agent(aid) or err("agent 不存在", 404)
    status = payload.get("status")
    if status not in ("active", "paused", "archived"):
        err("status 非法")
    db.update_agent(aid, status=status)
    if status == "active":
        hub.poke()  # 恢复后立刻补送积压消息
    await ws_manager.broadcast({"t": "agent", "id": aid, "run": hub.run_state(aid)})
    await ws_manager.broadcast({"t": "convs_changed"})
    return {"agent": agent_view(db.get_agent(aid))}


@app.post("/api/agents/{aid}/stop")
async def api_agent_stop(aid: int):
    await hub.stop_agent(aid)
    return {"ok": True}


@app.post("/api/agents/{aid}/interrupt")
async def api_agent_interrupt(aid: int):
    ok = await hub.interrupt_agent(aid)
    return {"ok": ok}


@app.get("/api/skills")
async def api_skills():
    return {
        "library": skills.list_library(),
        "global": skills.list_global(),
        "library_dir": config.SKILLS_DIR,
        "global_dir": config.GLOBAL_SKILLS_DIR,
    }


@app.post("/api/open_folder")
async def api_open_folder(payload: dict = Body(...)):
    # 只允许打开这两个技能目录，别的路径不开（本地工具，防手滑）
    path = payload.get("path") or ""
    if path not in (config.SKILLS_DIR, config.GLOBAL_SKILLS_DIR):
        err("只允许打开技能目录")
    os.makedirs(path, exist_ok=True)
    os.startfile(path)
    return {"ok": True}


@app.get("/api/stats")
async def api_stats(hours: float = 5):
    return db.usage_stats(time.time() - hours * 3600)


@app.get("/api/usage")
def api_usage():  # 同步 def：订阅接口是阻塞网络调用，FastAPI 会丢线程池跑
    return {
        "subscription": usage.subscription_usage(),
        "local": db.usage_stats(time.time() - 5 * 3600),
    }


# ---------------- 越权授权流 ----------------
# agent 开了"越权询问"后，claude CLI 遇到权限不足的操作会调 mcp__chat__ask_permission，
# 请求挂在这里等用户在界面上点允许/拒绝（超时按拒绝）。

_perm_reqs = {}   # id -> {"future": asyncio.Future, "info": {...}}
_perm_seq = 0


async def _handle_ask_permission(agent, args):
    import asyncio
    global _perm_seq
    _perm_seq += 1
    rid = _perm_seq
    tool_name = args.get("tool_name") or "?"
    tool_input = args.get("input") or {}
    info = {
        "id": rid, "agent_id": agent["id"], "agent": agent["name"],
        "tool": tool_name,
        "input_summary": json.dumps(tool_input, ensure_ascii=False)[:400],
    }
    fut = asyncio.get_event_loop().create_future()
    _perm_reqs[rid] = {"future": fut, "info": info}
    await ws_manager.broadcast({"t": "perm", "req": info})
    try:
        allow = await asyncio.wait_for(fut, timeout=config.PERMISSION_ASK_TIMEOUT)
    except asyncio.TimeoutError:
        allow = False
    finally:
        _perm_reqs.pop(rid, None)
        await ws_manager.broadcast({"t": "perm_done", "id": rid})
    if allow:
        return json.dumps({"behavior": "allow", "updatedInput": tool_input})
    return json.dumps({"behavior": "deny",
                       "message": f"{config.USER_NAME} 拒绝了这次操作（或未在时限内响应）。换个不需要该权限的做法，或在聊天里说明你为什么需要它。"})


@app.get("/api/permissions")
async def api_permissions():
    return {"pending": [r["info"] for r in _perm_reqs.values()]}


@app.post("/api/permissions/{rid}/answer")
async def api_perm_answer(rid: int, payload: dict = Body(...)):
    r = _perm_reqs.get(rid)
    if r and not r["future"].done():
        r["future"].set_result(bool(payload.get("allow")))
    return {"ok": True}


@app.post("/api/shutdown")
async def api_shutdown():
    import asyncio
    asyncio.get_event_loop().call_later(0.3, os._exit, 0)
    return {"ok": True}


@app.post("/api/agents/{aid}/dm")
async def api_agent_dm(aid: int):
    a = db.get_agent(aid) or err("agent 不存在", 404)
    dm = db.ensure_dm(db.USER, ("agent", aid))
    return {"conv_id": dm["id"]}


# ---------------- 会话 ----------------

@app.get("/api/convs")
async def api_convs(scope: str = "mine"):
    return {"convs": db.all_conversations() if scope == "all" else db.user_conversations()}


@app.post("/api/convs")
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


@app.get("/api/convs/{cid}")
async def api_conv_detail(cid: int):
    c = db.get_conv(cid) or err("会话不存在", 404)
    return {"conv": db.conv_summary(c)}


@app.get("/api/convs/{cid}/messages")
async def api_messages(cid: int, before_id: int = 0, limit: int = 50):
    db.get_conv(cid) or err("会话不存在", 404)
    msgs, has_more = db.list_messages(cid, before_id or None, min(limit, 200))
    return {"messages": msgs, "has_more": has_more}


@app.post("/api/convs/{cid}/send")
async def api_send(cid: int, payload: dict = Body(...)):
    db.get_conv(cid) or err("会话不存在", 404)
    if not db.is_member(cid, "user", 0):
        err("你不在这个会话里，先加入才能发言")
    text = (payload.get("text") or "").strip()
    if not text:
        err("消息不能为空")
    msg = db.post_message(cid, "user", 0, text)
    await hub.chain_clear(cid)  # 用户发言会重置链长
    await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    hub.poke()
    return {"message": msg}


@app.post("/api/convs/{cid}/read")
async def api_read(cid: int, payload: dict = Body(...)):
    db.mark_read(db.USER, cid, int(payload.get("last_id") or 0))
    await ws_manager.broadcast({"t": "read", "conv_id": cid})
    return {"ok": True}


@app.post("/api/convs/{cid}/members")
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


@app.post("/api/convs/{cid}/settings")
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


@app.post("/api/convs/{cid}/chain_reset")
async def api_chain_reset(cid: int):
    db.get_conv(cid) or err("会话不存在", 404)
    msg = db.post_message(cid, "system", 0, f"{config.USER_NAME} 允许 agent 继续对话", kind="chain_reset")
    await hub.chain_clear(cid)
    await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
    hub.poke()
    return {"ok": True}


# ---------------- agent 的工具接口（MCP 桥转发到这里） ----------------

class ToolError(Exception):
    pass


async def _tool_dispatch(agent, tool, args):
    aid = agent["id"]
    me = ("agent", aid)

    if tool == "send_message":
        cid = int(args["conversation_id"])
        if not db.is_member(cid, "agent", aid):
            raise ToolError(f"你不在会话 {cid} 里。用 list_conversations 查看你的会话，或用 open_dm 私聊。")
        text = (args.get("text") or "").strip()
        if not text:
            raise ToolError("消息不能为空")
        msg = db.post_message(cid, "agent", aid, text)
        await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
        hub.poke()
        st = db.chain_state(db.get_conv(cid))
        note = ""
        if st["paused"]:
            note = f"（提醒：该会话 agent 连续发言已达上限 {st['limit']}，在 {config.USER_NAME} 发言前你们的消息不会再互相送达）"
        return f"已发送到会话 {cid}（消息 id={msg['id']}）{note}"

    if tool == "list_conversations":
        out = []
        for s in db.agent_conversations(aid):
            out.append({
                "conversation_id": s["id"], "type": s["type"], "name": s["display_name"],
                "members": [m["name"] for m in s["members"]],
            })
        return out or "你目前不在任何会话里。可以用 open_dm 找人私聊。"

    if tool == "read_messages":
        cid = int(args["conversation_id"])
        if not db.is_member(cid, "agent", aid):
            raise ToolError(f"你不在会话 {cid} 里")
        msgs, has_more = db.list_messages(cid, args.get("before_id") or None, int(args.get("limit") or 30))
        out = [{"id": m["id"], "sender": m["sender"] or "(系统)",
                "time": time.strftime("%m-%d %H:%M", time.localtime(m["created_at"])),
                "text": m["content"]} for m in msgs]
        return {"messages": out, "has_more": has_more}

    if tool == "open_dm":
        target = (args.get("with") or "").strip()
        if target.lower() in ("user", config.USER_NAME.lower()) or target == config.USER_NAME:
            other = db.USER
        else:
            t = db.get_agent_by_name(target)
            if not t:
                raise ToolError(f"没有叫「{target}」的 agent。用 list_agents 看看都有谁。")
            if t["id"] == aid:
                raise ToolError("不能和自己私聊")
            other = ("agent", t["id"])
        dm = db.ensure_dm(me, other)
        await ws_manager.broadcast({"t": "convs_changed"})
        return f"私聊已就绪，conversation_id={dm['id']}，用 send_message 发言"

    if tool == "create_group":
        name = (args.get("name") or "").strip() or "新群聊"
        members = [me]
        if args.get("include_user", True):
            members.append(db.USER)
        for n in args.get("members") or []:
            t = db.get_agent_by_name(n)
            if not t:
                raise ToolError(f"没有叫「{n}」的 agent")
            members.append(("agent", t["id"]))
        cid = db.create_conversation("group", name, agent["name"], members)
        msg = db.post_message(cid, "system", 0, f"「{agent['name']}」创建了群聊「{name}」", kind="sys")
        await ws_manager.broadcast({"t": "convs_changed"})
        await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
        return f"群聊已创建，conversation_id={cid}"

    if tool == "add_member":
        cid = int(args["conversation_id"])
        if not db.is_member(cid, "agent", aid):
            raise ToolError(f"你不在会话 {cid} 里")
        c = db.get_conv(cid)
        if c["type"] == "dm":
            raise ToolError("私聊不能拉人，用 create_group 建群")
        t = db.get_agent_by_name((args.get("agent") or "").strip())
        if not t:
            raise ToolError("没有这个 agent")
        if db.is_member(cid, "agent", t["id"]):
            return f"「{t['name']}」已经在群里了"
        db.add_member(cid, "agent", t["id"])
        msg = db.post_message(cid, "system", 0, f"「{agent['name']}」邀请「{t['name']}」加入了群聊", kind="sys")
        await ws_manager.broadcast({"t": "convs_changed"})
        await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
        return f"已把「{t['name']}」拉进会话 {cid}"

    if tool == "leave_conversation":
        cid = int(args["conversation_id"])
        if not db.is_member(cid, "agent", aid):
            raise ToolError(f"你不在会话 {cid} 里")
        c = db.get_conv(cid)
        if c["type"] == "dm":
            raise ToolError("私聊不能退出")
        db.remove_member(cid, "agent", aid)
        msg = db.post_message(cid, "system", 0, f"「{agent['name']}」退出了群聊", kind="sys")
        await ws_manager.broadcast({"t": "convs_changed"})
        await ws_manager.broadcast({"t": "msg", "conv_id": cid, "message": msg})
        return f"已退出会话 {cid}"

    if tool == "list_agents":
        out = []
        for a in db.list_agents():
            if a["status"] == "archived" or a["id"] == aid:
                continue
            out.append({"name": a["name"], "status": a["status"], "memo": a["memo"]})
        return out or "系统里暂时没有其他 agent。"

    raise ToolError(f"未知工具: {tool}")


def _piggyback(agent):
    """agent 正在干活时到达的消息，搭工具返回值的便车送进它的上下文。

    这就是"中途补充指令"的实现：不用打断进程、零额外唤醒成本，
    agent 下一次碰任何聊天工具就能看到你的新话。"""
    pend = db.agent_pending(agent)
    if not pend["batches"]:
        return ""
    blocks = []
    for b in pend["batches"]:
        db.set_delivered(("agent", agent["id"]), b["conv"]["id"], b["msgs"][-1]["id"])
        blocks.append(prompts.batch_block(b["conv"], b["member_names"], b["msgs"]))
    return prompts.piggyback_block(blocks)


@app.post("/internal/tool")
async def internal_tool(payload: dict = Body(...)):
    agent = db.get_agent_by_token(payload.get("token") or "")
    if not agent:
        return JSONResponse({"ok": False, "error": "无效的 agent token"})
    tool = payload.get("tool")
    if tool == "ask_permission":
        # 授权应答必须是纯 JSON，不能混入捎带消息
        result = await _handle_ask_permission(agent, payload.get("args") or {})
        return JSONResponse({"ok": True, "result": result})
    try:
        result = await _tool_dispatch(agent, tool, payload.get("args") or {})
        extra = _piggyback(agent)
        if extra:
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=1)
            result += extra
        return JSONResponse({"ok": True, "result": result})
    except ToolError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except (KeyError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": f"参数错误: {e}"})


# ---------------- WebSocket ----------------

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
app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
