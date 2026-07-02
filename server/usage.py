"""订阅用量：读 Claude Code 本地保存的 OAuth token，问官方接口拿
Session(5h)/Weekly 等窗口的百分比——就是 /usage 命令显示的那份数据。

注意这是非公开接口，随时可能变；所以全程防御式处理，
拿不到就返回 None，界面上直接不显示这一栏。
"""
import json
import os
import time
import urllib.request

CRED_PATH = os.path.expanduser(os.path.join("~", ".claude", ".credentials.json"))
_cache = {"ts": 0, "data": None}

LABELS = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly (7d)",
    "seven_day_opus": "Weekly Opus",
    "seven_day_oauth_apps": "Weekly OAuth apps",
}


def _label(key):
    return LABELS.get(key) or key.replace("seven_day_", "Weekly ").replace("_", " ").title()


def subscription_usage():
    """返回 [{key,label,utilization,resets_at}] 或 None。60 秒缓存，避免频繁打接口。"""
    if time.time() - _cache["ts"] < 60:
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
            rows = []
            for key, val in raw.items():
                if isinstance(val, dict) and val.get("utilization") is not None:
                    rows.append({
                        "key": key,
                        "label": _label(key),
                        "utilization": val.get("utilization"),
                        "resets_at": val.get("resets_at"),
                    })
            data = rows or None
    except Exception:
        data = None
    _cache.update(ts=time.time(), data=data)
    return data
