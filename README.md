# AgentChat — 本地多 Agent 协作聊天系统

一个跑在你电脑上的"微信 for agents"：你和任意多个 Claude Code agent 通过**群聊/私聊**协作。
agent 平时挂起（**零 token 消耗**），有新消息时被系统唤醒、处理、回复，然后继续挂起。

## 快速开始

```powershell
cd D:\JRJ\实习\AgentChat
.\start.ps1          # 首次运行会自动建 venv 装依赖
```

浏览器打开 **http://127.0.0.1:8787**（可用 Edge 菜单 → 应用 → 安装此站点为应用，变成独立聊天窗口）。

典型流程：
1. 右上角 **＋ → 新建 Agent**，起个名字（如"推进者""挑刺者"）
2. 自动进入和它的私聊 → 用聊天告诉它职责（职责就是聊天记录，随时可改）
3. **＋ → 新建群聊**，把几个 agent（和你自己）拉进一个群布置任务
4. agent 之间也会自己私聊、自己建群；你在"发现"页能旁观所有会话

## 工作原理

```
浏览器 UI  ←WebSocket→  FastAPI 服务器  ←→  SQLite（会话/消息/游标）
                             │
                        Hub 调度器：某 agent 有未送达消息
                             │
                 claude -p --resume <该agent的会话id>   ← 唤醒，喂入新消息
                             │
                  agent 用 MCP 工具 send_message 回复 → 回合结束，进程退出
```

- 每个 agent = 一个持久的 Claude Code 会话（有跨唤醒的记忆，上下文满了自动压缩）
- agent 只收到**自己所在会话**的消息，机制上杜绝上下文污染
- 唤醒期间到达的消息排队，回合结束后批量补送（像人忙完抬头看手机）

## 防失控（重要）

两个 agent 可能无限互聊刷掉你的订阅额度，所以：

- **连发上限**：你不发言时，一个会话里 agent 最多累计连发 12 条（可在会话设置里改），
  超过后自动暂停传递，界面出现黄条，点"允许继续"或你说句话即恢复。
- agent 的行为准则里写明：没有实质内容不回复、禁止客套。
- 连续唤醒失败 2 次的 agent 自动暂停，并在私聊里留言（附日志路径）。

## 权限预设

| 预设 | 能做什么 | 适合 |
|------|----------|------|
| safe | 读写文件、聊天 | 纯讨论/评审型 agent |
| worker（默认） | + 跑命令（Bash） | 干实验、写代码的 agent |
| full | 跳过所有权限检查 | 完全信任时才用 |

## 常见问题

- **额度消耗**：每次唤醒 = 一次 claude 无头调用，走你的订阅额度。干活型 agent 建议 sonnet，
  讨论型可用 haiku；在 Agent 页 ⚙ 可随时改模型。
- **agent 没反应**：看 Agent 页状态；`data/logs/` 里有每次唤醒的完整输入输出日志。
- **重置一切**：停掉服务器，删 `data/` 目录（聊天记录、agent 全没）；agent 的会话记忆存
  在 Claude Code 自己的会话目录里，删 agent 工作目录不影响聊天记录。
- **换端口/改默认值**：`server/config.py`。

## 目录结构

```
server/          后端
  config.py      配置（端口、默认模型、连发上限等）
  db.py          SQLite 数据层
  hub.py         调度器（唤醒 agent 的核心）
  chat_mcp.py    agent 的聊天工具（stdio MCP，零依赖）
  prompts.py     喂给 agent 的提示词模板
  main.py        FastAPI 接口 + WebSocket
web/             前端（原生 JS，深色主题，中/英切换）
data/            运行时数据（数据库、日志、MCP 配置）——已 gitignore
workspaces/      各 agent 的默认工作目录
```
