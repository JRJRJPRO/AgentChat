"""技能分发：把 AgentChat 技能库里的 skill 按勾选"链接"进各 agent 的工作目录。

背景（Claude Code 的 skill 查找规则）：
- 全局技能 ~/.claude/skills/<name>/SKILL.md —— 任何目录启动的 claude 都能看到，
  所以天生对所有 agent 可见，不需要分发。
- 项目技能 <工作目录>/.claude/skills/<name>/SKILL.md —— 只有在该目录干活的 claude 看得到。

AgentChat 的做法：BASE_DIR/skills/ 是"技能库"（想给部分 agent 用的技能放这里），
勾选后用 NTFS junction 链接到 <agent工作目录>/.claude/skills/<name>——
改库里的文件，所有链接过去的 agent 即时生效；取消勾选只删链接不删原文件。
"""
import os
import re
import shutil

from . import config


def _read_meta(skill_dir):
    """从 SKILL.md 抠 name/description（frontmatter 优先，没有就取第一行正文）。"""
    path = os.path.join(skill_dir, "SKILL.md")
    desc = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(4000)
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        if m:
            desc = m.group(1).strip().strip("\"'")
        else:
            body = re.sub(r"^---.*?---\s*", "", text, flags=re.S)
            for line in body.splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    desc = line
                    break
    except OSError:
        pass
    return desc


def _scan(root):
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "SKILL.md")):
            out.append({"name": name, "description": _read_meta(d)})
    return out


def list_library():
    return _scan(config.SKILLS_DIR)


def list_global():
    return _scan(config.GLOBAL_SKILLS_DIR)


def _link(src, dst):
    """优先 junction（免管理员、改原文件即时生效），不行再退化成复制。"""
    try:
        import _winapi
        _winapi.CreateJunction(src, dst)
        return "junction"
    except Exception:
        pass
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except OSError:
        shutil.copytree(src, dst)
        return "copy"


def _is_our_link(path):
    """只删指回技能库的链接，agent 自己目录里的真技能绝不动。"""
    try:
        real = os.path.realpath(path)
        return os.path.normcase(real).startswith(os.path.normcase(os.path.realpath(config.SKILLS_DIR)) + os.sep)
    except OSError:
        return False


def sync_agent_skills(agent, names):
    """让 agent 的 .claude/skills 与勾选列表一致。返回实际生效的技能名列表。"""
    lib = {s["name"] for s in list_library()}
    want = [n for n in names if n in lib]
    target_root = os.path.join(agent["cwd"], ".claude", "skills")
    os.makedirs(target_root, exist_ok=True)

    for name in want:
        dst = os.path.join(target_root, name)
        if not os.path.exists(dst):
            _link(os.path.join(config.SKILLS_DIR, name), dst)

    # 之前勾过、现在取消的：只清掉我们建的链接
    prev = [n for n in (agent.get("skills") or "").split(",") if n]
    for name in prev:
        if name in want:
            continue
        dst = os.path.join(target_root, name)
        if os.path.exists(dst) and _is_our_link(dst):
            try:
                os.rmdir(dst)          # junction / 目录符号链接
            except OSError:
                shutil.rmtree(dst)     # 复制出来的目录
    return want
