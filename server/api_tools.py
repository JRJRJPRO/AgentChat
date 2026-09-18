"""agent 工具接口：/internal/tool 派发（chat_mcp 桥转发到这里）+ 越权授权流 + 提问选择卡。"""
import asyncio
import json
import time

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from . import config, db, prompts
from .runtime import err, hub, ws_manager

router = APIRouter()


class ToolError(Exception):
    pass


# ---------------- 越权授权流 ----------------
# agent 开了"越权询问"后，claude CLI 遇到权限不足的操作会调 mcp__chat__ask_permission，
# 请求挂在这里等用户在界面上点允许/拒绝（超时按拒绝）。

_perm_reqs = {}   # id -> {"future": asyncio.Future, "info": {...}}
_perm_seq = 0


async def _handle_ask_permission(agent, args):
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


@router.get("/api/permissions")
async def api_permissions():
    return {"pending": [r["info"] for r in _perm_reqs.values()]}


@router.post("/api/permissions/{rid}/answer")
async def api_perm_answer(rid: int, payload: dict = Body(...)):
    r = _perm_reqs.get(rid)
    if r and not r["future"].done():
        r["future"].set_result(bool(payload.get("allow")))
    return {"ok": True}


# ---------------- agent 提问选择卡（ask_user） ----------------
# agent 需要用户拍板时调 mcp__chat__ask_user(question, options)，
# 在界面上弹选择卡片；用户点选项或手打回答，答案只回给发问的 agent 本人，
# 不写进聊天记录——群聊里其他 agent 的上下文不受任何污染。

_ask_reqs = {}
_ask_seq = 0


async def _handle_ask_user(agent, args):
    global _ask_seq
    question = (args.get("question") or "").strip()
    if not question:
        raise ToolError("question 不能为空")
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()][:8]
    if not options:
        raise ToolError("至少给一个选项（用户也可以手打自定义回答）")
    _ask_seq += 1
    rid = _ask_seq
    info = {"id": rid, "agent_id": agent["id"], "agent": agent["name"],
            "question": question[:600], "options": options,
            # 截止时刻，前端画倒计时用；超时后 agent 按自己判断继续
            "expires_at": time.time() + config.ASK_USER_TIMEOUT}
    fut = asyncio.get_event_loop().create_future()
    _ask_reqs[rid] = {"future": fut, "info": info}
    await ws_manager.broadcast({"t": "ask", "req": info})
    try:
        # 分段等并每轮重查截止时刻：用户点「取消倒计时」会把 expires_at 推后
        while True:
            remain = info["expires_at"] - time.time()
            if remain <= 0:
                answer = None
                break
            try:
                # shield：分段超时不能把真正的回答 future 一起取消掉
                answer = await asyncio.wait_for(asyncio.shield(fut), timeout=min(remain, 5))
                break
            except asyncio.TimeoutError:
                continue
    finally:
        _ask_reqs.pop(rid, None)
        await ws_manager.broadcast({"t": "ask_done", "id": rid, "agent": agent["name"],
                                    "reason": "answered" if answer else "timeout"})
    if answer is None:
        return f"{config.USER_NAME} 暂时没有回答（可能不在电脑前）。按你的最佳判断继续，必要时在聊天里留言说明你选了什么、为什么。"
    return f"{config.USER_NAME} 的回答：{answer}"


@router.get("/api/asks")
async def api_asks():
    return {"pending": [r["info"] for r in _ask_reqs.values()]}


@router.post("/api/asks/{rid}/answer")
async def api_ask_answer(rid: int, payload: dict = Body(...)):
    r = _ask_reqs.get(rid)
    if r and not r["future"].done():
        r["future"].set_result(str(payload.get("answer") or "").strip()[:2000] or None)
    return {"ok": True}


@router.post("/api/asks/{rid}/hold")
async def api_ask_hold(rid: int):
    """用户点「取消倒计时」：截止时刻推后（够慢慢打字），上限受 MCP HTTP 超时约束。"""
    r = _ask_reqs.get(rid) or err("提问已结束", 404)
    r["info"]["expires_at"] = time.time() + config.ASK_USER_HOLD_SECONDS
    r["info"]["held"] = True
    await ws_manager.broadcast({"t": "ask_hold", "id": rid, "expires_at": r["info"]["expires_at"]})
    return {"ok": True}


# ---------------- 工具派发 ----------------

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
        # include_notes=False：观察层记录（思考留痕/系统提醒）只给用户看，不进模型上下文
        msgs, has_more = db.list_messages(cid, args.get("before_id") or None, int(args.get("limit") or 30),
                                          include_notes=False)
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

    if tool == "ask_user":
        return await _handle_ask_user(agent, args)

    if tool == "list_agents":
        out = []
        for a in db.list_agents():
            if a["status"] == "archived" or a["id"] == aid:
                continue
            out.append({"name": a["name"], "status": a["status"], "memo": a["memo"]})
        return out or "系统里暂时没有其他 agent。"

    if tool == "set_reminder":
        # 跨挂起的闹钟：会话内轮询活不过收工，由服务器保管、到点发消息唤醒
        try:
            minutes = float(args.get("minutes"))
        except (TypeError, ValueError):
            raise ToolError("minutes 要是数字（分钟）")
        minutes = max(1.0, min(minutes, 7 * 24 * 60))
        note = (args.get("note") or "").strip()[:500]
        if not note:
            raise ToolError("note 必填：到点收到的消息里只有它能告诉你该干什么")
        cmd = (args.get("check_command") or "").strip()[:1000]
        due = time.time() + minutes * 60
        recheck = int(max(60, min(minutes * 60, 1800)))  # 没好就隔 1-30 分钟重查
        rid = db.add_reminder(agent["id"], due, note, cmd, recheck, time.time() + 7 * 24 * 3600)
        # 观察层 ⏰ 留痕：老板能看到谁挂了什么提醒
        dm = db.ensure_dm(db.USER, ("agent", agent["id"]))
        txt = (f"「{agent['name']}」设了提醒：{minutes:g} 分钟后"
               + (f"，先跑检查命令通过才唤醒（{cmd[:120]}）" if cmd else "")
               + f" —— {note}")
        m = db.post_message(dm["id"], "note", agent["id"], txt, kind="remind")
        await ws_manager.broadcast({"t": "msg", "conv_id": dm["id"], "message": m})
        due_str = time.strftime("%m-%d %H:%M", time.localtime(due))
        return (f"提醒已由服务器登记（id={rid}），{due_str} 到点"
                + ("；届时先跑你的检查命令，退出码 0 才唤醒你，否则自动顺延重查（最长 7 天后强制唤醒）。"
                   if cmd else "，届时系统消息会唤醒你。")
                + "现在可以放心收工结束本轮。")

    raise ToolError(f"未知工具: {tool}")


def _piggyback(agent):
    """agent 正在干活时到达的消息，搭工具返回值的便车送进它的上下文。

    这就是"中途补充指令"的实现：不用打断进程、零额外唤醒成本，
    agent 下一次碰任何聊天工具就能看到你的新话。
    返回 (捎带文本, [(conv_id, 送达前游标, 送达到的消息id), ...])，
    给 ✓ 回执广播、过程卡换段和"已读不回"补救用。"""
    pend = db.agent_pending(agent)
    if not pend["batches"]:
        return "", []
    blocks, receipts = [], []
    for b in pend["batches"]:
        cid = b["conv"]["id"]
        prev = db.get_cursor(("agent", agent["id"]), cid)["last_delivered_id"]
        db.set_delivered(("agent", agent["id"]), cid, b["msgs"][-1]["id"])
        receipts.append((cid, prev, b["msgs"][-1]["id"]))
        blocks.append(prompts.batch_block(b["conv"], b["member_names"], b["msgs"]))
    return prompts.piggyback_block(blocks), receipts


@router.post("/internal/tool")
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
        pig, receipts = _piggyback(agent)
        for cid, prev_cur, upto in receipts:
            await ws_manager.broadcast({"t": "receipt", "conv_id": cid,
                                        "agent_id": agent["id"], "upto": upto})
            await hub.on_piggyback(agent["id"], cid, prev_cur, upto)
        extra = pig + await hub.usage_note(agent["id"])
        if extra:
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=1)
            result += extra
        return JSONResponse({"ok": True, "result": result})
    except ToolError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except (KeyError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": f"参数错误: {e}"})
