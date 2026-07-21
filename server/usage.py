"""订阅用量：读 Claude Code 本地保存的 OAuth token，问官方接口拿
Session(5h)/Weekly 等窗口的百分比——就是 /usage 命令显示的那份数据。

注意这是非公开接口，随时可能变；所以全程防御式处理，
拿不到就返回 None，界面上直接不显示这一栏。
"""
import json
import os
import time
import urllib.error
import urllib.request

CRED_PATH = os.path.expanduser(os.path.join("~", ".claude", ".credentials.json"))
_cache = {"ts": 0, "data": None}
# 2026-07-21 实测：这个元数据接口自己也会限流（HTTP 429 rate_limit_error）。
# 那不是"格式变了"，退避重试就好：退避期间沿用旧数据，间隔指数翻倍到最多 1 小时
_rl = {"until": 0, "delay": 300}
_err = {"kind": None, "ts": 0}  # kind: None=正常 | "rate_limited" | "error"

LABELS = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly (7d)",
    "seven_day_opus": "Weekly Opus",
    "seven_day_oauth_apps": "Weekly OAuth apps",
}


def _label(key):
    return LABELS.get(key) or key.replace("seven_day_", "Weekly ").replace("_", " ").title()


def subscription_usage():
    """返回 [{key,label,utilization,resets_at}] 或 None。60 秒缓存；
    被限流(429)时退避并沿用旧数据（新旧程度看 status()['data_ts']）。"""
    now = time.time()
    if now - _cache["ts"] < 60 or now < _rl["until"]:
        return _cache["data"]
    data = None
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            creds = json.load(f)
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if token:
            req = urllib.request.Request(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            data = _parse_limits(raw) or _parse_legacy(raw)
    except urllib.error.HTTPError as e:
        if e.code == 429:  # 接口限流：退避重试，旧数据先顶着
            _rl.update(until=now + _rl["delay"], delay=min(_rl["delay"] * 2, 3600))
            _err.update(kind="rate_limited", ts=now)
            return _cache["data"]
        _err.update(kind="error", ts=now)
    except Exception:
        _err.update(kind="error", ts=now)
    if data is not None:
        _rl.update(until=0, delay=300)
        _err.update(kind=None, ts=now)
    _cache.update(ts=now, data=data)
    return data


def status():
    """给监控/界面看的健康状况：最近错误类型、数据新旧、限流退避到何时。"""
    return {"kind": _err["kind"], "err_ts": _err["ts"],
            "data_ts": _cache["ts"], "retry_at": _rl["until"]}


def session_usage():
    """取 Session(5h) 窗口那一行 {label, utilization, resets_at}；查询失败返回 None。
    给用量预警监控用——它只关心 5 小时窗口。"""
    rows = subscription_usage()
    if not rows:
        return None
    for r in rows:
        if r["key"].startswith(("session", "five_hour")):
            return r
    return None


KIND_LABELS = {
    "session": "Session (5h)",
    "weekly_all": "Weekly (7d)",
}


def _parse_limits(raw):
    """新版接口：limits 数组，每项 {kind, percent, resets_at, scope}。
    weekly_scoped 带 scope.model.display_name（如 Fable/Opus），拼成 "Weekly Fable"。"""
    limits = raw.get("limits")
    if not isinstance(limits, list):
        return None
    rows = []
    for item in limits:
        if not isinstance(item, dict) or item.get("percent") is None:
            continue
        kind = item.get("kind") or ""
        label = KIND_LABELS.get(kind)
        if not label:
            scope = item.get("scope") or {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            surface = scope.get("surface") if isinstance(scope.get("surface"), dict) else {}
            name = model.get("display_name") or surface.get("display_name")
            if name:
                label = f"Weekly {name}" if item.get("group") == "weekly" else name
            else:
                label = kind.replace("_", " ").title() or "Unknown"
        rows.append({
            "key": f"{kind}:{label}",
            "label": label,
            "utilization": item.get("percent"),
            "resets_at": item.get("resets_at"),
        })
    return rows or None


def _parse_legacy(raw):
    """旧版接口：顶层 {five_hour:{utilization,...}, seven_day:{...}, ...}"""
    rows = []
    for key, val in raw.items():
        if isinstance(val, dict) and val.get("utilization") is not None and key not in ("extra_usage", "spend"):
            rows.append({
                "key": key,
                "label": _label(key),
                "utilization": val.get("utilization"),
                "resets_at": val.get("resets_at"),
            })
    return rows or None
