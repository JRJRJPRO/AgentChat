"""资源库路由：记忆/技能包的查看与网页编辑、收编、拆分。
注：按 v4 规划该功能将在 v4.2 下线（记忆回归 CC 原生方式、技能共享 ~/.claude/skills），
届时整个文件删除。"""
import os
import shutil

from fastapi import APIRouter, Body

from . import config, db, memories, skills
from .runtime import err

router = APIRouter()


def _lib_root(kind):
    if kind == "memories":
        return config.MEMORIES_DIR
    if kind == "skills":
        return config.SKILLS_DIR
    if isinstance(kind, str) and kind.startswith("agent:"):
        # agent 的工作记忆：<工作目录>/memory——它自建/自改的包在资源库里可看可编辑
        try:
            a = db.get_agent(int(kind.split(":", 1)[1]))
        except ValueError:
            a = None
        if not a:
            err("agent 不存在", 404)
        return os.path.join(a["cwd"], "memory")
    err("kind 只能是 memories / skills / agent:<id>")


_ILLEGAL_CHARS = set('/:*?"<>|' + chr(92))


def _lib_path(kind, pack, fname=None):
    """校验并拼出库内路径；pack/文件名不允许包含路径分隔符等，防穿越。"""
    root = _lib_root(kind)
    for part in ([pack, fname] if fname else [pack]):
        if not part or part in (".", "..") or any(c in _ILLEGAL_CHARS for c in part):
            err("名字含非法字符")
    p = os.path.abspath(os.path.join(root, pack, fname) if fname else os.path.join(root, pack))
    if not p.startswith(os.path.abspath(root) + os.sep):
        err("路径越界")
    return p


@router.get("/api/library")
async def api_library():
    mems = memories.list_library()
    sks = skills.list_library()
    for s in sks:
        d = os.path.join(config.SKILLS_DIR, s["name"])
        s["files"] = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    agents = [a for a in db.list_agents() if a["status"] != "archived"]
    for m in mems:
        m["used_by"] = [a["name"] for a in agents if m["name"] in (a.get("memories") or "").split(",")]
    for s in sks:
        s["used_by"] = [a["name"] for a in agents if s["name"] in (a.get("skills") or "").split(",")]
    # 各在岗 agent 工作目录里的实际记忆包（含它们自建的）——记忆的事实源
    agent_mems = []
    for a in agents:
        packs = memories.list_workspace(a)
        if packs:
            agent_mems.append({"agent_id": a["id"], "agent_name": a["name"], "packs": packs})
    return {"memories": mems, "skills": sks, "agent_mems": agent_mems,
            "memories_dir": config.MEMORIES_DIR, "skills_dir": config.SKILLS_DIR}


@router.post("/api/library/read")
async def api_library_read(payload: dict = Body(...)):
    path = _lib_path(payload.get("kind"), payload.get("pack"), payload.get("file"))
    if not os.path.isfile(path):
        err("文件不存在", 404)
    if os.path.getsize(path) > 512 * 1024:
        err("文件太大，请用本地编辑器打开")
    with open(path, encoding="utf-8", errors="replace") as f:
        return {"content": f.read()}


@router.post("/api/library/save")
async def api_library_save(payload: dict = Body(...)):
    path = _lib_path(payload.get("kind"), payload.get("pack"), payload.get("file"))
    if not os.path.isfile(path):
        err("文件不存在（新建请走新建文件入口）", 404)
    with open(path, "w", encoding="utf-8") as f:
        f.write(payload.get("content") or "")
    return {"ok": True}


_MEM_TEMPLATE = """一句话描述这个记忆包（本行会显示在勾选列表里）

- [示例条目](example.md) — 把长内容拆成单独文件，这里放索引
"""

_SKILL_TEMPLATE = """---
description: 一句话描述这个技能
---

在这里写技能内容。
"""


@router.post("/api/library/promote")
async def api_library_promote(payload: dict = Body(...)):
    """把某 agent 工作目录里的记忆包"收编"进中央记忆库（复制一份，可再分发给其他 agent）。
    原 agent 继续用自己的工作副本；顺手把包名记进它的勾选，防止 auto_mount
    因"已入库未勾选"而把它卸载。"""
    kind = payload.get("kind") or ""
    pack = (payload.get("pack") or "").strip()
    if not kind.startswith("agent:"):
        err("只能收编 agent 的工作记忆")
    src = _lib_path(kind, pack)
    dst = _lib_path("memories", pack)
    if not os.path.isfile(os.path.join(src, "MEMORY.md")):
        err("包不存在", 404)
    if os.path.exists(dst):
        err("中央记忆库已存在同名包")
    shutil.copytree(src, dst)
    a = db.get_agent(int(kind.split(":", 1)[1]))
    mems = [n for n in (a.get("memories") or "").split(",") if n]
    if pack not in mems:
        db.update_agent(a["id"], memories=",".join(mems + [pack]))
    return {"ok": True}


@router.post("/api/library/new_pack")
async def api_library_new_pack(payload: dict = Body(...)):
    kind = payload.get("kind")
    if kind not in ("memories", "skills"):
        err("新建包只支持中央库（agent 的工作记忆由它自己在工作目录里建）")
    path = _lib_path(kind, (payload.get("name") or "").strip())
    if os.path.exists(path):
        err("已存在同名包")
    os.makedirs(path)
    if kind == "memories":
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write(_MEM_TEMPLATE)
    else:
        with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(_SKILL_TEMPLATE)
    return {"ok": True}


@router.post("/api/library/split")
async def api_library_split(payload: dict = Body(...)):
    """把记忆包里勾选的条目拆成新包（只支持 memories；skills 是共享链接不适用）。"""
    if payload.get("kind") != "memories":
        err("只有记忆包支持拆分")
    pack = payload.get("pack") or ""
    new_name = (payload.get("new_name") or "").strip()
    _lib_path("memories", pack)       # 复用非法字符/越界校验
    _lib_path("memories", new_name)
    try:
        out = memories.split_pack(pack, payload.get("files") or [],
                                  new_name, payload.get("description") or "")
    except ValueError as e:
        err(str(e))
    return {"ok": True, **out}


@router.post("/api/library/new_file")
async def api_library_new_file(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name.lower().endswith(".md"):
        name += ".md"
    path = _lib_path(payload.get("kind"), payload.get("pack"), name)
    if not os.path.isdir(os.path.dirname(path)):
        err("包不存在", 404)
    if os.path.exists(path):
        err("已存在同名文件")
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    return {"ok": True, "file": name}
