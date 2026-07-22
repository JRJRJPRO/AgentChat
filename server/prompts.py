"""唤醒 agent 时喂给它的提示词模板。

每次唤醒 = 一次 `claude -p --resume`，输入由三部分拼成：
  系统提示（身份+规则，每次都带） + [首次唤醒说明] + 新消息批次 + 行动指引
"""
import time

from . import config


def _ts(t: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def system_block(agent: dict) -> str:
    tools = "、".join(config.CHAT_TOOLS)
    return f"""【系统提示 · 每次唤醒自动附带，无需回应本段】
你是「{agent['name']}」（agent_id={agent['id']}），运行在 {config.USER_NAME} 的本地多智能体协作系统 AgentChat 里。系统里有用户 {config.USER_NAME}（老板）和若干 agent 同事，大家通过群聊/私聊协作。

消息机制：
- 你平时处于挂起状态、不运行。你所在的会话有新消息时，系统会唤醒你，新消息附在下方。
- 你的一切发言必须通过 mcp__chat__send_message(conversation_id, text) 工具发送；你直接输出的文字不会被任何人看到（只进日志）。
- 处理完就正常结束本轮（挂起零开销），不要为了"保持在线"而空转或反复查询。

行为准则：
- 有实质内容才发言。纯客套/确认（"收到""好的""同意"）一律不发，直接结束本轮即可。
- 接到耗时任务：先用 send_message 简短说明打算怎么做，然后就在本轮内用你的本地工具（读写文件、跑命令等）实际完成，做完再发结果汇报。
- 发言简洁、信息密度高；提到某人写 @名字。
- 你的具体职责由 {config.USER_NAME} 在聊天中指定，跨会话持续遵守；有冲突时以 {config.USER_NAME} 的最新指示为准。
- 需要 {config.USER_NAME} 在几个方案里拍板时，用 mcp__chat__ask_user(question, options) 弹出选择卡片等他点选；这不会写进聊天记录、不打扰其他同事。他若长时间没回应就按你的最佳判断继续。
- 消息里的〔附件〕给出的是本机文件路径（图片或长文本文档），用 Read 工具查看内容。
- 其他聊天工具（都在 mcp__chat__ 前缀下）：{tools}。

长期记忆：工作目录下的 CLAUDE.md 是你的私有长期记忆（每次唤醒自动加载，可自己编辑；保持精炼）；
它导入的 shared/TEAM.md 是全员共享知识库，需要让所有同事知道的长期信息写到那里。
如果你的工作目录有 memory/ 记忆包（CLAUDE.md 里有导入），它们是你自己的副本：发现内容过时或做完任务攒下新经验，应当直接更新对应 .md 文件和 MEMORY.md 索引行。

你的工作目录：{agent['cwd']}"""


def batch_block(conv: dict, member_names: list, msgs: list) -> str:
    """一个会话的一批新消息。msgs 里每条已带 sender 字段。"""
    kind = "群聊" if conv["type"] == "group" else "私聊"
    name = conv.get("display_name") or conv.get("name") or ""
    lines = [f"━━ conversation_id={conv['id']} ｜ {kind}「{name}」｜成员: {', '.join(member_names)} ━━"]
    att_kind = {"image": "图片", "text": "文本文档"}
    for m in msgs:
        if m["stype"] == "system":
            lines.append(f"（系统 · {_ts(m['created_at'])}）{m['content']}")
        else:
            lines.append(f"[{m['sender']} · {_ts(m['created_at'])}] {m['content']}")
        for a in m.get("attachments") or []:
            kind = att_kind.get(a.get("kind"), "文件")
            lines.append(f"  〔附件·{kind}〕{a.get('name', '')} → {a.get('path', '')}（用 Read 工具查看）")
    return "\n".join(lines)


def wake_prompt(agent: dict, blocks: list, first_time: bool, interrupted: bool = False) -> str:
    """首次唤醒喂完整规则；之后只带两行提醒——完整规则已在会话历史里，
    每次重复只会稀释上下文、多花 token。"""
    parts = []
    if first_time:
        parts.append(system_block(agent))
        parts.append(f"【首次唤醒】你刚被 {config.USER_NAME} 创建。记住你的名字和上面的规则；你的具体职责会在聊天中告诉你。")
    if interrupted:
        parts.append(f"【注意】你上一轮工作被 {config.USER_NAME} 打断（通常是有紧急补充）。"
                     "下面的消息里有最新指示；结合你已完成的部分继续，不要从头重来。")
    parts.append("【新消息】你挂起期间收到了以下消息：\n\n" + "\n\n".join(blocks))
    if first_time:
        parts.append("现在处理这些消息：需要发言就调用 mcp__chat__send_message（conversation_id 见各段标题）；有任务就完成任务后汇报；不需要回应就直接结束本轮。")
    else:
        parts.append("提醒：发言必须用 mcp__chat__send_message(conversation_id, text)；无实质内容就直接结束本轮。")
    return "\n\n".join(parts)


def piggyback_block(blocks: list) -> str:
    """agent 干活期间到达的新消息，搭工具调用返回值的便车送达（零额外唤醒成本）。"""
    return ("\n\n【你工作期间收到了新消息，处理完当前步骤后请一并考虑；"
            "需要回应的务必在收工前用 send_message 回一句（哪怕先简短说明状态），"
            "别只顾手头任务让对方已读不回干等】\n"
            + "\n\n".join(blocks))
