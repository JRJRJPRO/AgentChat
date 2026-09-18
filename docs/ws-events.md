# WebSocket 事件契约（前后端唯一接口协议）

> 服务端广播 JSON（`{"t": <类型>, ...}`），前端在 `web/js/ws.js` 的 `connectWS` 里分发。
> **改任何一侧都必须同步本表**——这张表就是防止前后端悄悄漂移成屎山的锚点。
> 客户端 → 服务端方向只有心跳文本 `"ping"`（内容被忽略）。

| t | 字段 | 何时广播 | 前端反应 |
|---|---|---|---|
| `msg` | `conv_id`, `message`(完整消息对象) | 任何新消息入库（用户/agent/system/note） | 追加气泡、未读数、通知；note 不进预览/未读 |
| `msg_update` | `conv_id`, `message` | 观察层过程卡定格/换段（内容被改写） | 原位重绘该 note 节点 |
| `agent` | `id`, `run`(`idle\|working\|waiting\|compacting\|probing`) | 运行状态变化 | 更新状态点/列表/输入区提示；working 且原空闲时清空过程动态 |
| `ctx` | `id`, `ctx`(tokens, -1=已压缩待测), `win`, `at` | 唤醒结束/压缩/📊探测得到新上下文数 | 更新私聊头部与详情卡 |
| `act` | `id`, `item`({ts,k:"note"\|"tool",...}) | 唤醒进程流出一条思考/工具调用 | 追加进直播中的 💭 过程卡 |
| `auth` | `needed`(bool), `kind`(`auth\|limit`), `agent`, `detail`, `resets_at?` | 登录失效/用量打满全局挂起、及其解除 | 顶部横幅出现/消失 |
| `ask` | `req`({id,agent,question,options,expires_at}) | agent 调 ask_user 提问 | 中央浮层加选择卡 |
| `ask_done` | `id`, `agent`, `reason`(`answered\|timeout`) | 提问被回答或超时 | 移除卡片；超时弹 toast |
| `ask_hold` | `id`, `expires_at` | 用户点「取消倒计时」 | 卡片改不限时 |
| `perm` | `req`({id,agent,tool,input_summary}) | agent 请求越权操作 | 右下角授权卡 |
| `perm_done` | `id` | 授权已被应答/超时 | 移除授权卡 |
| `usage_alert` | `alert`({pct,resets_at,threshold} 或 null), `fresh` | 订阅用量过阈值/回落/重置撤警 | 设置区显示；fresh 时 toast+通知 |
| `usage_fail` | `failing`, `reason`(`rate_limited\|error`), `retry_at?` | 用量接口限流/连续失败、及恢复 | 限流只在设置区显示；真失败 toast |
| `chain` | `conv_id`, `paused`(bool) | 会话链长达到上限暂停/被重置 | 会话内横幅 |
| `receipt` | `conv_id`, `agent_id`, `upto`(消息id) | agent 投递游标移动（推进=送达，回退=收回） | ✓ 回执刷新 |
| `convs_changed` | — | 会话/成员/agent 结构性变化 | 全量 refreshLists() |
| `read` | `conv_id` | 用户已读游标推进 | 清未读角标 |

维护规则：新增事件先加表再写码；字段只增不改语义；废弃事件保留一行标 ~~删除线~~ 及废弃版本号。
