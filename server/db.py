"""SQLite 数据层：agent、会话、成员、消息、游标。

设计要点：
- 群聊/私聊统一为 conversation（type: group|dm），成员可以是用户(mtype='user', mid=0)或 agent。
- 每个成员在每个会话里有两个游标：last_read_id（用户未读角标用）、last_delivered_id（agent 派送用）。
- "链长"= 用户上次发言（或 chain_reset）之后 agent 累计发言数，超过上限就暂停自动派送，防止 agent 互聊刷额度。
- 所有函数同步（sqlite 足够快），一把全局锁保证线程安全；时间存 unix 秒。
"""
import sqlite3
import threading
import time
import uuid
import secrets

from . import config

_lock = threading.RLock()
_con: sqlite3.Connection = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  session_id TEXT NOT NULL,
  token TEXT NOT NULL,
  cwd TEXT NOT NULL,
  model TEXT NOT NULL,
  permission TEXT NOT NULL,
  memo TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  bootstrapped INTEGER NOT NULL DEFAULT 0,
  wake_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  last_wake_at REAL
);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  name TEXT,
  chain_limit INTEGER,
  created_by TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
  conv_id INTEGER NOT NULL,
  mtype TEXT NOT NULL,
  mid INTEGER NOT NULL,
  joined_at REAL NOT NULL,
  PRIMARY KEY(conv_id, mtype, mid)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id INTEGER NOT NULL,
  stype TEXT NOT NULL,
  sid INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'text',
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);
CREATE TABLE IF NOT EXISTS cursors(
  mtype TEXT NOT NULL,
  mid INTEGER NOT NULL,
  conv_id INTEGER NOT NULL,
  last_read_id INTEGER NOT NULL DEFAULT 0,
  last_delivered_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(mtype, mid, conv_id)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wakes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  started REAL NOT NULL,
  ended REAL,
  rc INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_creation INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  num_turns INTEGER NOT NULL DEFAULT 0,
  log_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_wakes_time ON wakes(started);
"""

USER = ("user", 0)


def init():
    global _con
    config.ensure_dirs()
    _con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _con.row_factory = sqlite3.Row
    _con.execute("PRAGMA journal_mode=WAL")
    with _lock:
        _con.executescript(SCHEMA)
        _con.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
        _con.commit()
    _migrate()
    _sweep_orphans()


def _migrate():
    """老库补新列（新库建表时也走这里，幂等）。"""
    cols = {r["name"] for r in _rows("PRAGMA table_info(agents)")}
    if "extra_dirs" not in cols:
        _exec("ALTER TABLE agents ADD COLUMN extra_dirs TEXT NOT NULL DEFAULT ''")
    if "skills" not in cols:
        _exec("ALTER TABLE agents ADD COLUMN skills TEXT NOT NULL DEFAULT ''")
    if "ask_perm" not in cols:
        _exec("ALTER TABLE agents ADD COLUMN ask_perm INTEGER NOT NULL DEFAULT 0")
    if "memories" not in cols:
        _exec("ALTER TABLE agents ADD COLUMN memories TEXT NOT NULL DEFAULT ''")
    if "ctx_tokens" not in cols:
        # 最近一次唤醒结束时的上下文长度（result 事件 iterations[-1] 的 input+cache 读写和）；
        # 0 = 未知，-1 = 刚压缩过（等 /context 探测或下次唤醒重新统计）
        _exec("ALTER TABLE agents ADD COLUMN ctx_tokens INTEGER NOT NULL DEFAULT 0")
        _exec("ALTER TABLE agents ADD COLUMN ctx_window INTEGER NOT NULL DEFAULT 0")
    if "ctx_at" not in cols:
        # ctx_tokens 的统计时刻（唤醒结束/压缩后探测/📊 查询），界面显示"这个数是什么时候量的"
        _exec("ALTER TABLE agents ADD COLUMN ctx_at REAL NOT NULL DEFAULT 0")
    mcols = {r["name"] for r in _rows("PRAGMA table_info(messages)")}
    if "attachments" not in mcols:
        # 附件（图片/临时文档）存 JSON 数组：[{kind,name,path,url,size}, ...]
        _exec("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT ''")


def _sweep_orphans():
    """启动时完整性清扫（幂等）：
    1. members/cursors 里指向已不存在 agent 的行 -> 删（界面不再显示 agent#N）
    2. 无消息且成员已失效的会话（零成员，或 dm 只剩一方）-> 连带删除
       注意：有消息的会话一律保留（群聊只删无效成员行，群本身不动）。"""
    _exec("DELETE FROM members WHERE mtype='agent' AND mid NOT IN (SELECT id FROM agents)")
    _exec("DELETE FROM cursors WHERE mtype='agent' AND mid NOT IN (SELECT id FROM agents)")
    for c in _rows("SELECT * FROM conversations"):
        mems = _rows("SELECT * FROM members WHERE conv_id=?", (c["id"],))
        broken = (not mems) or (c["type"] == "dm" and len(mems) < 2)
        if not broken:
            continue
        has_msg = _row("SELECT 1 x FROM messages WHERE conv_id=? LIMIT 1", (c["id"],))
        if has_msg:
            continue
        _exec("DELETE FROM cursors WHERE conv_id=?", (c["id"],))
        _exec("DELETE FROM members WHERE conv_id=?", (c["id"],))
        _exec("DELETE FROM conversations WHERE id=?", (c["id"],))
    # 孤儿游标：会话已不存在的 cursor 行
    _exec("DELETE FROM cursors WHERE conv_id NOT IN (SELECT id FROM conversations)")


def _rows(sql, args=()):
    with _lock:
        return [dict(r) for r in _con.execute(sql, args).fetchall()]


def _row(sql, args=()):
    rs = _rows(sql, args)
    return rs[0] if rs else None


def _exec(sql, args=()):
    with _lock:
        cur = _con.execute(sql, args)
        _con.commit()
        return cur.lastrowid


# ---------------- meta（键值设置） ----------------

def get_meta(key, default=None):
    r = _row("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def set_meta(key, value):
    _exec("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


# ---------------- agents ----------------

def create_agent(name, cwd, model, permission, memo=""):
    aid = _exec(
        "INSERT INTO agents(name,session_id,token,cwd,model,permission,memo,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (name, str(uuid.uuid4()), secrets.token_hex(16), cwd, model, permission, memo, time.time()),
    )
    return get_agent(aid)


def get_agent(aid):
    return _row("SELECT * FROM agents WHERE id=?", (aid,))


def get_agent_by_name(name):
    return _row("SELECT * FROM agents WHERE name=?", (name,))


def get_agent_by_token(token):
    return _row("SELECT * FROM agents WHERE token=?", (token,))


def list_agents():
    return _rows("SELECT * FROM agents ORDER BY id")


def update_agent(aid, **kw):
    if not kw:
        return
    sets = ",".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE agents SET {sets} WHERE id=?", (*kw.values(), aid))


def record_wake(aid):
    _exec("UPDATE agents SET wake_count=wake_count+1, last_wake_at=? WHERE id=?", (time.time(), aid))


def add_wake(agent_id, started, rc, usage, log_path):
    """记一次唤醒的用量。usage 来自 claude -p 的 JSON 输出，可能缺字段。"""
    u = usage or {}
    _exec(
        """INSERT INTO wakes(agent_id,started,ended,rc,input_tokens,output_tokens,
           cache_read,cache_creation,cost_usd,num_turns,log_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (agent_id, started, time.time(), rc,
         u.get("input_tokens", 0), u.get("output_tokens", 0),
         u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0),
         u.get("cost_usd", 0.0), u.get("num_turns", 0), log_path),
    )


def usage_stats(since_ts):
    total = _row(
        """SELECT COUNT(*) wakes, COALESCE(SUM(input_tokens),0) input_tokens,
           COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cache_read),0) cache_read,
           COALESCE(SUM(cache_creation),0) cache_creation, COALESCE(SUM(cost_usd),0) cost_usd,
           COALESCE(SUM(num_turns),0) num_turns
           FROM wakes WHERE started>=?""",
        (since_ts,),
    )
    names = agent_names()
    per_agent = _rows(
        """SELECT agent_id, COUNT(*) wakes, COALESCE(SUM(output_tokens),0) output_tokens,
           COALESCE(SUM(cost_usd),0) cost_usd FROM wakes WHERE started>=?
           GROUP BY agent_id ORDER BY cost_usd DESC""",
        (since_ts,),
    )
    for r in per_agent:
        r["name"] = names.get(r["agent_id"], f"agent#{r['agent_id']}")
    return {"total": total, "per_agent": per_agent}


def agent_names():
    return {r["id"]: r["name"] for r in _rows("SELECT id,name FROM agents")}


def sender_name(stype, sid):
    if stype == "user":
        return config.USER_NAME
    if stype == "system":
        return ""
    return agent_names().get(sid, f"agent#{sid}")


# ---------------- conversations & members ----------------

def create_conversation(ctype, name, created_by, members):
    """members: [(mtype, mid), ...]"""
    cid = _exec(
        "INSERT INTO conversations(type,name,created_by,created_at) VALUES(?,?,?,?)",
        (ctype, name, created_by, time.time()),
    )
    for mt, mi in members:
        add_member(cid, mt, mi)
    return cid


def get_conv(cid):
    return _row("SELECT * FROM conversations WHERE id=?", (cid,))


def update_conv(cid, **kw):
    if not kw:
        return
    sets = ",".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE conversations SET {sets} WHERE id=?", (*kw.values(), cid))


def add_member(cid, mt, mi):
    _exec("INSERT OR IGNORE INTO members VALUES(?,?,?,?)", (cid, mt, mi, time.time()))
    # 新成员从"当前最新消息"开始接收，历史消息不会轰炸式补发（想看可以自己翻）
    last = _row("SELECT COALESCE(MAX(id),0) m FROM messages WHERE conv_id=?", (cid,))["m"]
    _exec("INSERT OR IGNORE INTO cursors VALUES(?,?,?,?,?)", (mt, mi, cid, last, last))


def remove_member(cid, mt, mi):
    _exec("DELETE FROM members WHERE conv_id=? AND mtype=? AND mid=?", (cid, mt, mi))


def members_of(cid):
    return _rows("SELECT * FROM members WHERE conv_id=? ORDER BY joined_at", (cid,))


def is_member(cid, mt, mi):
    return _row("SELECT 1 x FROM members WHERE conv_id=? AND mtype=? AND mid=?", (cid, mt, mi)) is not None


def member_names(cid):
    names = agent_names()
    out = []
    for m in members_of(cid):
        out.append(config.USER_NAME if m["mtype"] == "user" else names.get(m["mid"], f"agent#{m['mid']}"))
    return out


def find_dm(a, b):
    return _row(
        """SELECT c.* FROM conversations c
           JOIN members m1 ON m1.conv_id=c.id AND m1.mtype=? AND m1.mid=?
           JOIN members m2 ON m2.conv_id=c.id AND m2.mtype=? AND m2.mid=?
           WHERE c.type='dm'""",
        (a[0], a[1], b[0], b[1]),
    )


def ensure_dm(a, b):
    """找到或创建 a、b 之间的私聊，返回 conversation 行。"""
    c = find_dm(a, b)
    if c:
        return c
    cid = create_conversation("dm", None, None, [a, b])
    return get_conv(cid)


# ---------------- messages ----------------

def post_message(cid, stype, sid, content, kind="text", attachments=None):
    import json as _json
    mid = _exec(
        "INSERT INTO messages(conv_id,stype,sid,kind,content,attachments,created_at) VALUES(?,?,?,?,?,?,?)",
        (cid, stype, sid, kind, content,
         _json.dumps(attachments, ensure_ascii=False) if attachments else "", time.time()),
    )
    if stype in ("user", "agent"):  # 自己发的自己视为已读、已送达
        bump_cursor(stype, sid, cid, mid)
    return get_message(mid)


def _parse_atts(m):
    import json as _json
    try:
        m["attachments"] = _json.loads(m["attachments"]) if m.get("attachments") else []
    except Exception:
        m["attachments"] = []
    return m


def get_message(mid):
    m = _row("SELECT * FROM messages WHERE id=?", (mid,))
    if m:
        m["sender"] = sender_name(m["stype"], m["sid"])
        _parse_atts(m)
    return m


def update_message(mid, content):
    """只给观察层 note 用：唤醒结束时把过程动态定格进占位消息。"""
    _exec("UPDATE messages SET content=? WHERE id=?", (content, mid))
    return get_message(mid)


def list_messages(cid, before_id=None, limit=50, include_notes=True):
    """返回 (按 id 升序的消息列表, has_more)。before_id 用于往上翻历史。
    include_notes=False 给 agent 的 read_messages 用：观察层记录不进模型上下文。"""
    note = "" if include_notes else " AND stype!='note'"
    if before_id:
        rs = _rows(f"SELECT * FROM messages WHERE conv_id=? AND id<?{note} ORDER BY id DESC LIMIT ?", (cid, before_id, limit + 1))
    else:
        rs = _rows(f"SELECT * FROM messages WHERE conv_id=?{note} ORDER BY id DESC LIMIT ?", (cid, limit + 1))
    has_more = len(rs) > limit
    rs = rs[:limit]
    names = agent_names()
    for m in rs:
        m["sender"] = config.USER_NAME if m["stype"] == "user" else ("" if m["stype"] == "system" else names.get(m["sid"], f"agent#{m['sid']}"))
        _parse_atts(m)
    return list(reversed(rs)), has_more


# ---------------- cursors ----------------

def get_cursor(viewer, cid):
    c = _row("SELECT * FROM cursors WHERE mtype=? AND mid=? AND conv_id=?", (viewer[0], viewer[1], cid))
    return c or {"mtype": viewer[0], "mid": viewer[1], "conv_id": cid, "last_read_id": 0, "last_delivered_id": 0}


def bump_cursor(mt, mi, cid, msg_id):
    _exec(
        """INSERT INTO cursors VALUES(?,?,?,?,?)
           ON CONFLICT(mtype,mid,conv_id) DO UPDATE SET
             last_read_id=MAX(last_read_id,excluded.last_read_id),
             last_delivered_id=MAX(last_delivered_id,excluded.last_delivered_id)""",
        (mt, mi, cid, msg_id, msg_id),
    )


def mark_read(viewer, cid, msg_id):
    _exec(
        """INSERT INTO cursors VALUES(?,?,?,?,0)
           ON CONFLICT(mtype,mid,conv_id) DO UPDATE SET last_read_id=MAX(last_read_id,excluded.last_read_id)""",
        (viewer[0], viewer[1], cid, msg_id),
    )


def agent_replied_after(aid, cid, after_id):
    """该 agent 在这个会话里、某条消息之后有没有发过言（"已读不回"检测用）。"""
    return _row("SELECT 1 x FROM messages WHERE conv_id=? AND stype='agent' AND sid=? AND id>? LIMIT 1",
                (cid, aid, after_id)) is not None


def set_delivered(viewer, cid, msg_id):
    """绝对设置（唤醒失败时用来回退，重新派送）。"""
    _exec(
        """INSERT INTO cursors VALUES(?,?,?,0,?)
           ON CONFLICT(mtype,mid,conv_id) DO UPDATE SET last_delivered_id=excluded.last_delivered_id""",
        (viewer[0], viewer[1], cid, msg_id),
    )


# ---------------- 链长（防 agent 互聊失控） ----------------

def chain_state(conv):
    limit = conv["chain_limit"] or config.DEFAULT_CHAIN_LIMIT
    anchor = _row(
        "SELECT COALESCE(MAX(id),0) a FROM messages WHERE conv_id=? AND (stype='user' OR kind='chain_reset')",
        (conv["id"],),
    )["a"]
    n = _row("SELECT COUNT(*) c FROM messages WHERE conv_id=? AND id>? AND stype='agent'", (conv["id"], anchor))["c"]
    return {"count": n, "limit": limit, "paused": n >= limit}


# ---------------- 会话摘要（侧栏列表用） ----------------

def conv_summary(conv, viewer=USER):
    cid = conv["id"]
    names = agent_names()
    mems = []
    for m in members_of(cid):
        nm = config.USER_NAME if m["mtype"] == "user" else names.get(m["mid"], f"agent#{m['mid']}")
        d = {"mtype": m["mtype"], "mid": m["mid"], "name": nm}
        if m["mtype"] == "agent":  # 已送达游标：给"✓ 谁收到了"回执用
            d["delivered_id"] = get_cursor(("agent", m["mid"]), cid)["last_delivered_id"]
        mems.append(d)
    im = any(m["mtype"] == viewer[0] and m["mid"] == viewer[1] for m in mems)

    if conv["type"] == "dm":
        others = [m for m in mems if not (m["mtype"] == viewer[0] and m["mid"] == viewer[1])]
        display = others[0]["name"] if (im and len(others) == 1) else " & ".join(m["name"] for m in mems)
    else:
        display = conv["name"] or "群聊"

    # note（观察层）不参与侧栏预览和未读数：它是过程留痕，不该把会话顶亮
    last = _row("SELECT * FROM messages WHERE conv_id=? AND stype!='note' ORDER BY id DESC LIMIT 1", (cid,))
    if last:
        last["sender"] = config.USER_NAME if last["stype"] == "user" else ("" if last["stype"] == "system" else names.get(last["sid"], ""))

    unread = 0
    if im:
        cur = get_cursor(viewer, cid)
        unread = _row(
            "SELECT COUNT(*) c FROM messages WHERE conv_id=? AND id>? AND stype!='note' AND NOT(stype=? AND sid=?)",
            (cid, cur["last_read_id"], viewer[0], viewer[1]),
        )["c"]

    st = chain_state(conv)
    return {
        "id": cid, "type": conv["type"], "name": conv["name"], "display_name": display,
        "members": mems, "is_member": im, "unread": unread,
        "chain": st, "chain_limit": conv["chain_limit"],
        "last_msg": last, "last_ts": (last or {}).get("created_at") or conv["created_at"],
    }


def user_conversations():
    convs = _rows(
        "SELECT c.* FROM conversations c JOIN members m ON m.conv_id=c.id WHERE m.mtype='user' AND m.mid=0"
    )
    out = [conv_summary(c) for c in convs]
    out.sort(key=lambda s: s["last_ts"], reverse=True)
    return out


def all_conversations():
    out = [conv_summary(c) for c in _rows("SELECT * FROM conversations")]
    out.sort(key=lambda s: s["last_ts"], reverse=True)
    return out


def agent_conversations(aid):
    convs = _rows(
        "SELECT c.* FROM conversations c JOIN members m ON m.conv_id=c.id WHERE m.mtype='agent' AND m.mid=?",
        (aid,),
    )
    return [conv_summary(c, viewer=("agent", aid)) for c in convs]


# ---------------- 派送扫描（Hub 用） ----------------

def agent_pending(agent):
    """返回该 agent 的未派送消息批次。

    - 自己发的消息不算；只有系统消息也不值得唤醒（下次搭车送达）。
    - 链长超限的会话整体跳过（不派送、不推进游标），记入 paused。
    """
    aid = agent["id"]
    res = {"batches": [], "wake": False, "paused": []}
    convs = _rows(
        "SELECT c.* FROM conversations c JOIN members m ON m.conv_id=c.id WHERE m.mtype='agent' AND m.mid=?",
        (aid,),
    )
    for c in convs:
        cur = get_cursor(("agent", aid), c["id"])
        msgs = _rows(
            # stype='note' 是观察层记录（思考过程/系统提醒留痕），只给用户看，绝不派送给 agent
            "SELECT * FROM messages WHERE conv_id=? AND id>? AND stype!='note' AND NOT(stype='agent' AND sid=?) ORDER BY id",
            (c["id"], cur["last_delivered_id"], aid),
        )
        if not msgs:
            continue
        if not any(m["stype"] in ("user", "agent") for m in msgs):
            continue
        if chain_state(c)["paused"]:
            res["paused"].append(c["id"])
            continue
        names = agent_names()
        for m in msgs:
            m["sender"] = config.USER_NAME if m["stype"] == "user" else ("" if m["stype"] == "system" else names.get(m["sid"], f"agent#{m['sid']}"))
            _parse_atts(m)
        c["display_name"] = conv_summary(c, viewer=("agent", aid))["display_name"]
        res["batches"].append({"conv": c, "msgs": msgs, "member_names": member_names(c["id"])})
        res["wake"] = True
    return res
