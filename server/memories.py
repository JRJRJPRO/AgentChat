"""记忆分发：把 memories/ 记忆库里的记忆包按勾选"复制"进各 agent 的工作目录。

与 skills 的区别（John 的选择：独立演化）：
- skills 用 junction 链接，库里一份、全员同步；
- memories 用复制副本：勾选时把 memories/<包名>/ 整个复制到 <agent工作目录>/memory/<包名>/，
  之后各 agent 自己改自己的，互不影响；已存在的副本绝不覆盖（保护 agent 自己演化的内容）。
- 勾选生效方式：在 agent 的 CLAUDE.md 里维护一个标记块，内含 @memory/<包名>/MEMORY.md 导入，
  每次唤醒自动进上下文；取消勾选只删导入行，副本文件保留。

记忆包格式：memories/<包名>/MEMORY.md（索引，第一行会被当作描述展示）+ 若干 .md 文件。
"""
import os
import re
import shutil

from . import config

MARK_START = "<!-- agentchat:memories:start -->"
MARK_END = "<!-- agentchat:memories:end -->"


def _read_meta(pack_dir):
    """取 MEMORY.md 第一行有内容的文字当描述。"""
    path = os.path.join(pack_dir, "MEMORY.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(2000)
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return ""


def list_library():
    out = []
    root = config.MEMORIES_DIR
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "MEMORY.md")):
            files = sorted(f for f in os.listdir(d)
                           if os.path.isfile(os.path.join(d, f)) and f.lower().endswith(".md"))
            out.append({"name": name, "description": _read_meta(d), "files": files})
    return out


def list_workspace(agent):
    """agent 工作目录 memory/ 下实际存在的记忆包（含它自己新建的）。
    这是该 agent 记忆的事实源：分发只是"复制进来"，之后它自己怎么建怎么改都算数。"""
    out = []
    root = os.path.join(agent["cwd"], "memory")
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "MEMORY.md")):
            files = sorted(f for f in os.listdir(d)
                           if os.path.isfile(os.path.join(d, f)) and f.lower().endswith(".md"))
            out.append({"name": name, "description": _read_meta(d), "files": files})
    return out


def auto_mount(agent):
    """把 CLAUDE.md 的记忆导入块对齐到工作目录实况（每次唤醒前调用）：
    - agent 自建的包（中央库里没有的名字）→ 一律挂载：它 mkdir 完下次唤醒就生效，
      不存在"以为建了记忆、系统不认"；
    - 中央库分发的包 → 勾选着才挂载（保留"取消勾选=卸载导入、副本保留"的老语义）。
    内容没变化就不写盘。返回挂载的包名列表。"""
    lib = {m["name"] for m in list_library()}
    checked = {n for n in (agent.get("memories") or "").split(",") if n}
    names = [p["name"] for p in list_workspace(agent)
             if p["name"] not in lib or p["name"] in checked]
    _rewrite_imports(agent, names)
    return names


def split_pack(pack, files, new_name, description=""):
    """把一个记忆包里勾选的若干条目拆出去成新包（John 的渐进式拆分：一次拆 2-4 个，
    不够以后对子包再拆）。移动 .md 文件本体 + MEMORY.md 里对应的索引行；
    索引里的相对链接是同目录的，跟着文件一起搬所以不会断。
    源包保留剩余条目；已分发到各 agent 的旧副本不动（John 拍板：后续分发用新的即可）。"""
    src = os.path.join(config.MEMORIES_DIR, pack)
    dst = os.path.join(config.MEMORIES_DIR, new_name)
    if not os.path.isfile(os.path.join(src, "MEMORY.md")):
        raise ValueError("源包不存在")
    if os.path.exists(dst):
        raise ValueError("已存在同名包")
    files = [f for f in files if f.lower().endswith(".md") and f != "MEMORY.md"
             and os.path.isfile(os.path.join(src, f))]
    if not files:
        raise ValueError("没有可拆的条目")

    with open(os.path.join(src, "MEMORY.md"), encoding="utf-8", errors="replace") as f:
        src_lines = f.read().splitlines()
    moved_idx, kept = [], []
    for line in src_lines:
        # 索引行里 "](文件名)" 命中任一要搬的文件就跟着走
        if any(f"]({fn})" in line for fn in files):
            moved_idx.append(line)
        else:
            kept.append(line)

    os.makedirs(dst)
    for fn in files:
        shutil.move(os.path.join(src, fn), os.path.join(dst, fn))
    head = (description or "").strip() or f"从「{pack}」拆出的记忆包"
    with open(os.path.join(dst, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write(head + "\n\n" + "\n".join(moved_idx) + ("\n" if moved_idx else ""))
    with open(os.path.join(src, "MEMORY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(kept).rstrip("\n") + "\n")
    return {"moved": len(files), "new_pack": new_name}


def ensure_claude_md(agent):
    """agent 的两级长期记忆：工作目录 CLAUDE.md（私有）+ @导入 shared/TEAM.md（全员共享）。
    CLAUDE.md 每次启动必定全文进上下文（无头模式也是），比 memory 目录可靠。
    只在缺失时生成模板，已有的绝不覆盖。"""
    path = os.path.join(agent["cwd"], "CLAUDE.md")
    if os.path.exists(path):
        return
    os.makedirs(agent["cwd"], exist_ok=True)
    team = config.SHARED_TEAM_FILE.replace("\\", "/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"# 「{agent['name']}」的长期记忆\n\n"
            f"@{team}\n\n"
            "上面一行导入了团队共享知识库（全员共用；要改共享内容请编辑那个文件本身）。\n"
            "从这里往下是只属于你的长期知识：职责总结、John 的相关偏好、踩坑经验。\n"
            "本文件每次唤醒都自动进入你的上下文，你可以随时自己编辑更新——保持精炼。\n"
        )


def sync_agent_memories(agent, names):
    """让 agent 的记忆副本与勾选一致。返回实际生效的记忆包名列表。"""
    lib = {m["name"] for m in list_library()}
    want = [n for n in names if n in lib]
    mem_root = os.path.join(agent["cwd"], "memory")
    for name in want:
        dst = os.path.join(mem_root, name)
        if not os.path.exists(dst):
            shutil.copytree(os.path.join(config.MEMORIES_DIR, name), dst)
    agent = dict(agent, memories=",".join(want))  # 用新勾选算挂载，别拿 DB 里的旧值
    auto_mount(agent)
    return want


def _rewrite_imports(agent, names):
    """重写 CLAUDE.md 里的记忆导入块（标记块整体替换，其余内容不动）；没变化不写盘。"""
    ensure_claude_md(agent)
    path = os.path.join(agent["cwd"], "CLAUDE.md")
    with open(path, encoding="utf-8", errors="replace") as f:
        orig = f.read()
    text = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END) + r"\n?", "", orig, flags=re.S)
    text = text.rstrip("\n") + "\n"
    if names:
        lines = [MARK_START,
                 "以下是你的记忆包（在工作目录 memory/<包名>/ 下，是你自己的文件，可自由增改；"
                 "索引里的相对链接指向同目录下的文件，按需用 Read 查看，不会自动进上下文）。"
                 "干活中发现记忆过时或攒下新经验，随手更新对应 .md 并同步索引行（MEMORY.md 保持一行一条、当天日期可写行尾）。"
                 "要新建记忆包：直接建 memory/<新包名>/MEMORY.md（首行一句话描述）+ 若干条目 .md，"
                 "下次唤醒自动挂载，John 在资源库里也看得到，不要建到别处："]
        lines += [f"@memory/{n}/MEMORY.md" for n in names]
        lines.append(MARK_END)
        text += "\n" + "\n".join(lines) + "\n"
    if text != orig:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
