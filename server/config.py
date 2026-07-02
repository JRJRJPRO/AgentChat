"""全局配置：路径、端口、默认参数、权限预设。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "chat.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MCP_DIR = os.path.join(DATA_DIR, "mcp")
WORKSPACES_DIR = os.path.join(BASE_DIR, "workspaces")
WEB_DIR = os.path.join(BASE_DIR, "web")

HOST = "127.0.0.1"
PORT = 8787
HUB_URL = f"http://{HOST}:{PORT}"

USER_NAME = "John"          # agent 眼中用户的名字；界面里显示为"我"

DEFAULT_MODEL = "sonnet"
MODELS = ["sonnet", "opus", "haiku"]

DEFAULT_CHAIN_LIMIT = 12    # 用户不发话时，一个会话里 agent 最多累计连发多少条（防互聊刷额度）
WAKE_DEBOUNCE = 1.5         # 收到新消息后等这么久再唤醒，把连发的消息攒成一批（秒）
WAKE_COOLDOWN = 3.0         # 同一个 agent 两次唤醒之间的最小间隔（秒）
WAKE_TIMEOUT = 2 * 3600     # 单次唤醒最长运行时间（秒），超时杀进程
MAX_CONSEC_FAILURES = 2     # 连续唤醒失败这么多次就自动暂停该 agent

# 权限预设：控制被唤醒的 claude 进程能做什么
#   safe   —— 只能读文件/改文件/聊天，不能跑命令
#   worker —— 还能跑命令（Bash），适合干实验的 agent
#   full   —— 完全跳过权限检查（等同 --dangerously-skip-permissions），慎用
PERMISSION_PRESETS = {
    "safe":   {"mode": "acceptEdits",       "extra_allowed": []},
    "worker": {"mode": "acceptEdits",       "extra_allowed": ["Bash"]},
    "full":   {"mode": "bypassPermissions", "extra_allowed": []},
}
DEFAULT_PERMISSION = "worker"

# agent 可用的聊天工具（MCP server 名叫 chat）
CHAT_TOOLS = [
    "send_message", "list_conversations", "read_messages", "open_dm",
    "create_group", "add_member", "leave_conversation", "list_agents",
]
ALLOWED_CHAT_TOOLS = [f"mcp__chat__{t}" for t in CHAT_TOOLS]


def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR, MCP_DIR, WORKSPACES_DIR):
        os.makedirs(d, exist_ok=True)
