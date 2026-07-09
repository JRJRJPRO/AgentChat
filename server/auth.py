"""Claude CLI 登录状态：过期检测 + 一键拉起登录窗口。

背景：Claude Code 的订阅登录（OAuth）token 会定期过期。CLI 交互模式会自动
弹浏览器续期，但我们用 `claude -p` 无头唤醒 agent，过期时只会失败退出，
表现就是"唤醒失败"。这里做两件事：
1. find_auth_error(log_text)：从唤醒日志里识别"是登录问题"而不是别的错误；
2. launch_login()：开一个新终端窗口跑 `claude /login`（会自动跳转浏览器完成
   OAuth），用户完成后回界面点"重试"即可，无需再开别的软件手动折腾。
"""
import json
import os
import re
import subprocess
import time

CRED_PATH = os.path.expanduser(os.path.join("~", ".claude", ".credentials.json"))

# 唤醒日志里出现这些字样 → 判定为登录/鉴权问题（大小写不敏感）
_AUTH_PATTERNS = [
    r"please run /login",
    r"run `?/login`?",
    r"invalid api key",
    r"oauth token (has )?expired",
    r"token (has )?expired",
    r"refresh token",
    r"authentication[_ ]error",
    r"not logged in",
    r"credentials? (are )?invalid",
    r"api error:? 401",
    r"\b401 unauthorized",
]
_AUTH_RE = re.compile("|".join(_AUTH_PATTERNS), re.IGNORECASE)


def find_auth_error(text):
    """在日志文本里找登录问题的证据；找到返回附近的片段（给界面显示），否则 None。"""
    if not text:
        return None
    m = _AUTH_RE.search(text)
    if not m:
        return None
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()[:300]


def credentials_status():
    """读 CLI 本地保存的 OAuth 凭据，返回 {found, expires_at, expired}。
    注意 expiresAt 过期不代表一定要重新登录（CLI 有 refresh token 会自动续），
    只作参考信息；真正的判定以唤醒失败日志为准。"""
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth") or {}
        exp_ms = oauth.get("expiresAt") or 0
        return {
            "found": bool(oauth.get("accessToken")),
            "expires_at": exp_ms / 1000 if exp_ms else None,
            "expired": bool(exp_ms) and exp_ms / 1000 < time.time(),
        }
    except Exception:
        return {"found": False, "expires_at": None, "expired": None}


def launch_login(claude_path=None):
    """开一个新控制台窗口跑 claude /login（交互模式会执行 /login 并自动打开
    浏览器完成 OAuth）。万一 CLI 版本不吃这个参数，窗口里手动输 /login 也行。"""
    exe = claude_path or "claude"
    if os.name == "nt":
        # .cmd 壳必须经由 cmd 启动；/k 保住窗口让用户看到登录结果
        subprocess.Popen(
            ["cmd", "/k", exe, "/login"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", exe, "/login"])
    return True
