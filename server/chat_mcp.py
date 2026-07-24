"""聊天工具的 stdio MCP server（只依赖标准库）。

被唤醒的 claude 进程通过它收发消息：每个工具调用都转发到 Hub 的
POST /internal/tool 接口，用 AGENT_TOKEN 表明自己是哪个 agent。

MCP stdio 协议 = 每行一条 JSON-RPC 消息。只需实现：
  initialize / notifications(忽略) / tools/list / tools/call / ping
"""
import json
import os
import sys
import urllib.request

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8787")
TOKEN = os.environ.get("AGENT_TOKEN", "")

TOOLS = [
    {
        "name": "send_message",
        "description": "在某个会话里发言（群聊或私聊）。这是你唯一能被别人看到的发言方式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "integer", "description": "会话 id（唤醒消息的标题里有）"},
                "text": {"type": "string", "description": "消息内容，支持 markdown"},
            },
            "required": ["conversation_id", "text"],
        },
    },
    {
        "name": "list_conversations",
        "description": "列出你所在的全部会话（id、类型、名称、成员）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_messages",
        "description": "翻某个会话的历史消息（按时间倒着翻页）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "integer"},
                "before_id": {"type": "integer", "description": "只取该消息 id 之前的，用于翻更早的历史"},
                "limit": {"type": "integer", "description": "取几条，默认 30"},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "open_dm",
        "description": "打开与某人的私聊（没有就自动创建），返回 conversation_id，之后用 send_message 发言。",
        "inputSchema": {
            "type": "object",
            "properties": {"with": {"type": "string", "description": "对方名字；找老板就填 user"}},
            "required": ["with"],
        },
    },
    {
        "name": "create_group",
        "description": "创建一个群聊并拉人。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "群名"},
                "members": {"type": "array", "items": {"type": "string"}, "description": "要拉的 agent 名字列表（自己不用填）"},
                "include_user": {"type": "boolean", "description": "是否把老板拉进群，默认 true"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_member",
        "description": "把某个 agent 拉进你所在的群聊。",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "integer"}, "agent": {"type": "string"}},
            "required": ["conversation_id", "agent"],
        },
    },
    {
        "name": "leave_conversation",
        "description": "退出某个群聊。",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "integer"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "list_agents",
        "description": "看看系统里有哪些 agent 同事（名字、状态、备注）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_user",
        "description": "向老板弹出一个选择卡片（问题+若干选项按钮），阻塞等他点选后返回所选答案。适合需要他拍板才能继续的场合。不会写进聊天记录、不打扰其他人；他长时间不回应会返回超时提示，届时按你的最佳判断继续。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问的问题，简洁一点"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "供点选的选项（2-6 个为宜）；老板也可以不选而是手打自定义回答"},
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "set_reminder",
        "description": (
            "设一个跨挂起的定时提醒（服务器保管的闹钟）。你每轮收工后进程就退出，"
            "会话里的一切轮询、后台任务完成通知、sleep 都会随之失效——凡是「等某事完成后继续」"
            "一律用本工具，设完就放心收工。到点后系统会用一条消息唤醒你（服务器重启也不丢）。"
            "可选 check_command：到点时服务器先在你的工作目录零成本跑这条命令，"
            "退出码 0 才唤醒你，非 0 则每隔一小会儿自动重查——适合「实验/长任务跑完叫我」，"
            "让命令去检查结果文件或进程状态。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "多少分钟后到点（1 到 10080，即最长 7 天）"},
                "note": {"type": "string", "description": "提醒内容备注：到点收到的消息里会原样带上，写清楚该干什么"},
                "check_command": {"type": "string", "description": "可选。到点时先跑的检查命令（shell，工作目录=你的工作目录）；退出码 0=条件满足才唤醒，非 0=没好，稍后自动重查"},
            },
            "required": ["minutes", "note"],
        },
    },
    {
        # 系统内部用：开了"越权询问"的 agent，CLI 遇到权限不足的操作时
        # 会自动调这个工具（--permission-prompt-tool），阻塞等用户点允许/拒绝。
        # agent 自己不要主动调它。
        "name": "ask_permission",
        "description": "（系统内部用）向用户请求越权操作的授权，请勿主动调用。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
    },
]


def call_hub(tool, args):
    req = urllib.request.Request(
        HUB_URL + "/internal/tool",
        data=json.dumps({"token": TOKEN, "tool": tool, "args": args}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # ask_permission / ask_user 要一直等到用户点按钮，其余工具 30 秒足够；
    # ask_user 支持「取消倒计时」（服务器端最多再等 1 小时），HTTP 超时要盖过它
    timeout = {"ask_permission": 660, "ask_user": 3700}.get(tool, 30)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("error", "unknown error"))
    return out.get("result")


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        mid = msg.get("id")
        if mid is None:  # notification，无需回复
            continue
        if method == "initialize":
            reply(mid, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "chat", "version": "1.0.0"},
            })
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                result = call_hub(params.get("name"), params.get("arguments") or {})
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1)
                reply(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:
                reply(mid, {"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True})
        elif method == "ping":
            reply(mid, {})
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
