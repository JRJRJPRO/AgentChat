# AgentChat v4 重构设计稿

> 2026-09-18，Agentchat修复1号。对应 John 的五点需求：记忆简化、effort 切换、
> API 接入 UI、对话树（rewind/分支）、打断语义重设计；外加"先把框架理顺，
> 修 bug 别堆成屎山"的总要求。**本稿是提案，实施前等 John 拍板末节的决策点。**

---

## 0. 技术可行性结论（先回答 John 的两个直接疑问）

### claude CLI / API 支持吗？

全部支持，且都是官方一等公民能力（`claude --help` 实测确认，CLI 2.1.276）：

| 需求 | 官方机制 | 说明 |
|---|---|---|
| 对话树分叉 | `--resume <sid> --fork-session` | resume 时另存新 session id，原会话文件不动。**纯本地转录文件复制，0 token**。新 session_id 从 stream-json 事件里读回（已在唤醒日志中确认存在） |
| effort 切换 | `--effort <low\|medium\|high\|xhigh\|max>` | CLI 直接一个 flag，逐次唤醒可不同 |
| 真·/btw（进阶，暂不做） | `--input-format stream-json` | 进程存活期间可持续注入用户消息 |

第三方兼容端点（Ling 等）：fork/树照常可用（转录管理全在本地 CLI 侧，与服务商无关）；`--effort` 只对官方 Claude 模型下发（第三方不一定认识，会被网关拒绝）。

### 切换分支会不会 token 命中为 0、用量激增？

**不会出现新的成本类别。** 关键事实：

1. Anthropic API 本来就是无状态的——**今天每次唤醒就已经在全量重发历史**了，你现在的每次 `--resume` 都是"从头推一遍上下文 + 缓存抵扣"。树不改变这一点。
2. 缓存是**前缀匹配**的：两个分支共享的前缀部分照样命中。Claude Code 的订阅流量走约 1 小时的缓存 TTL。
3. 切换动作本身 0 token（只是改指针，不发请求）。下次唤醒时：
   - 分支前缀在 TTL 内 → 命中缓存（命中部分按约一折计）；
   - 过期 → 一次全量 input——**和今天任何一个 agent 睡了 2 小时后被唤醒完全一样**，不是新增成本。
4. **回撤（rewind）反而省钱**：上下文变短，之后每一轮都更便宜。这正是比 /compact 更精准的减脂手段。

最坏情况 = 每次冷切换付一次"冷唤醒"的钱。频繁在两个都很长的分支间反复横跳（间隔 >1h）才会感觉贵，正常使用无感。

---

## 1. 总架构重构（v4.0，纯重构零行为变化）

### 现状病灶

- `hub.py` 1004 行上帝类：调度 + 子进程 + 用量监控 + 提醒 + 压缩 + 过程动态 + 登录挂起全在一起；
- agent 运行状态是散落的 flag 汤（`stopped/interrupt/compact/probe/timed_out/shown` + `interrupted` set + 全局 `auth_needed`），每加一个状态都要在七八处判断里穿针；
- `main.py` 983 行：路由 + 工具派发 + 授权流 + 资源库文件操作混住；
- `app.js` 1968 行单文件全局函数汤。

### 服务端拆分

```
server/
  main.py        装配层：FastAPI app、lifespan、挂路由/静态（~80 行）
  api_agents.py  agent CRUD/状态/打断/树操作 路由
  api_convs.py   会话/消息/附件/成员 路由
  api_system.py  state/设置/登录/用量/providers/关机 路由
  api_tools.py   /internal/tool 派发 + ask_user/ask_permission 流
  hub.py         只剩调度决策：唤醒扫描、状态机、链长（~300 行）
  runner.py      claude 子进程封装：命令拼装/env 注入/stream 泵/杀树/compact/probe
  treestore.py   对话树：节点表、fork 记账、head 指针、分支查询（v4.4 才有）
  usagemon.py    用量监控 + 限流全局挂起（从 hub 拆出）
  reminders.py   ⏰ 提醒
  providers.py   第三方模型：热重载 data/providers.json（不再 import 时一次性读死）
  db.py / prompts.py / auth.py / usage.py / chat_mcp.py   基本不动
  memories.py    缩水到 ~40 行（见 §2）
```

### 显式状态机（防屎山的核心）

agent 生命周期收敛为单一事实源，转移只发生在 hub 一处：

```
持久态（DB status）: active | held(被打断) | paused        ← archived 删除，见 §5
运行态（内存 run）  : idle | working | waiting | compacting | probing
全局态             : normal | auth_needed | limit_suspended   （独立轴，压所有唤醒）
```

| 转移 | 触发 |
|---|---|
| idle → working | 扫描发现该会话有值得唤醒的未派送消息 |
| working ⇄ waiting | 工具调用挂起 >2min / 事件恢复 |
| working → held | 用户点 ⏹ 打断（杀树 + 游标回退） |
| held → working | **打断之后**出现的新消息（老积压不触发） |
| working → paused | 连续失败 ×2 |
| any → idle | 进程正常收工 |

### 前端拆分（原生 ES modules，无构建步骤）

```
web/js/
  core.js      S 全局状态 / ws / api() / t()
  md.js        markdown 渲染三件套
  chat.js      聊天区、消息渲染、输入区
  agents.js    agent 列表、详情卡、编辑弹窗
  tree.js      对话树侧栏（v4.4）
  settings.js  设置弹窗（含模型接入）
  main.js      init 装配
```

`index.html` 只引 `<script type="module" src="js/main.js?v=NN">`。静态文件照旧刷新即生效。
WS 事件类型是前后端唯一契约，集中列成 `docs/ws-events.md` 一张表，改动必须同步。

### 回归验证

拆分期零行为变化，靠既有手段兜底：py_compile + 场景断言（DB 快照副本 + Hub.__new__ + 假 broadcast，v2.6 已验证此法可行）+ eslint no-undef（D:\tmp 复制法）+ 无头 Chrome 截图 + i18n parity。

---

## 2. 记忆系统简化（v4.2）

**原则：回到 Claude Code 原生方式，AgentChat 不再自建记忆管理。**

### 删除

- 中央记忆库 `memories/` 及"勾选分发/复制副本"机制；
- 资源库 UI 整个 tab；
- `/api/library/*` 全部 7 个端点（read/save/promote/split/new_pack/new_file/open_folder）；
- `agents.memories` 列及 create/update 里的同步逻辑；
- memories.py 里的 sync/split/promote/勾选对照逻辑。

### 保留（这些本来就是 CC 原生机制）

- 工作目录 `CLAUDE.md`（每次唤醒自动加载）+ `@shared/TEAM.md` 团队共享导入；
- 工作目录 `memory/` 文件——agent 自己建、自己改，`auto_mount` 缩水成一件事：
  扫 `memory/*/MEMORY.md`，全部保证有 `@` 导入行（不再对照中央库勾选）。

### "两边同步"怎么达成

agent 的记忆就是工作目录里的普通文件。John 自己在该目录开 `claude` 时读到**同一套**
CLAUDE.md + memory/，改了对 agent 下次唤醒立即生效——同步是天然的，不需要任何机制。
换电脑 = 拷 `workspaces/`（+ `data/chat.db` 若要聊天记录）。

另注：claude 进程自身还有 harness 级 auto-memory（`~/.claude/projects/<git根>/memory/`，
按仓库根分桶——所有 workspace 在同一仓库下会**共享一个桶**）。这是 CC 自己的机制，
我们不管理也不依赖它；agent 私有记忆的事实源始终是 workspace `memory/`。

### 迁移

现有各 agent 的 `memory/<包>/` 原样保留、全部继续挂载，无感迁移。中央 `memories/`
目录改名 `memories_retired/` 留档（不删文件），一个月后 John 确认无用再手动删。

### 技能库（已拍板 2026-09-18）

**与 Claude Code 共享同一个**：不是删功能，而是不再自建。AgentChat 的 skills/ 库、
junction 分发、按 agent 勾选全部下线；所有 agent 直接用 `~/.claude/skills`（和 John
自己开 CC 完全同一目录、同一套调用与更新方式）。AgentChat skills/ 里独有的包迁入全局
目录；个别 agent 专属技能手动放它工作目录 `.claude/skills/`。

---

## 3. effort 切换（v4.3，小改动）

- `agents` 表加 `effort TEXT DEFAULT ''`（空 = 跟 CLI 默认，即 xhigh）；
- 唤醒/压缩/探测命令统一追加 `--effort <v>`（仅官方 Claude 模型；第三方 provider 隐藏该选项）;
- 编辑弹窗 + 详情卡加下拉：默认 / low / medium / high / xhigh / max；
- 使用建议放 UI 提示里：日常协作 high 够用且省额度，攻坚 xhigh/max。

## 4. 新 API 快速接入（v4.3）

- 设置 ⚙ 新分区「模型接入」：列出 providers.json 现有条目（token 打码只显尾 4 位）；
  表单增/改/删：短名、model_id、base_url、auth_token、small_fast_model（选填）；
- 保存 → 写 `data/providers.json`（继续 gitignore）→ `providers.reload()` 热生效，
  新模型立即进所有下拉，**无需重启服务器**（当前 import 时读死的写法顺手治掉）；
- 「测试」按钮：后端向 `base_url/v1/messages` 发一个 max_tokens=1 的最小请求验证连通性
  和令牌有效性，返回 ok / 具体错误。

---

## 5. 打断语义重设计（v4.1）

### 现状（先把"混乱"讲清楚）

| 现有动作 | 实际行为 |
|---|---|
| 忙时直接发消息 | 排队；若 agent 中途调聊天工具会"捎带"进上下文（这其实就是 /btw，一直都有） |
| ⏸ 打断 | 杀进程 + 消息回退重发 → **下一轮扫描立即自动重唤醒**（"伪打断"的根源） |
| ⏹ 中止 | 杀进程 + 这批消息不再重发（这才接近你想象的打断，但按钮藏在 agent 列表） |

### 新语义（v4.1 起，2026-09-18 与 John 对齐）

先纠正一个常见误解：**CC 里按 Esc 不是"记忆全没"**——已完成的工具调用和结果都已写进
会话转录，打断只丢"在途未完成那一步"的思考，文件改动都在磁盘上，恢复后模型看得到
自己已完成的部分。"强制打断且中途开始"是 CC 原生行为，也是 AgentChat 现行打断的行为。

据此拆成两个不同动作：

- **忙时发消息 = 排队/捎带，不打断**（保持现状，输入框加提示文案"正在干活，消息将捎带送达"）；
- **⏹ 打断（半成品保留）**：杀进程树、游标回退（消息不丢）、agent 进 `held` 状态——
  **不再自动重唤醒**。半成品都在记忆里；打断**之后**来的任何新消息（含群聊 @ 它，
  已拍板取此默认）才重新唤醒，届时老积压 + 新消息合并送达，唤醒词带"你被打断了"
  标注，中途接着干不从头来；
- **↩ 撤回重编（Esc 语义）**：把你刚发的那条收回到输入框重新编辑。agent 还没醒
  （攒批窗口/排队中）→ 零成本撤回；已送达开工 → 等于打断 + 树回撤到上一节点
  （它记忆里当你没发过这条；磁盘上已做的改动保留）。完整形态依赖树（v4.4），
  v4.1 先做"未送达时可撤回"；
- 旧"打断/中止"两个按钮**合并为一个 ⏹**，出现在：私聊头部（该 agent working/waiting
  时）+ 详情悬浮卡 + 群聊成员卡——不用再去 agent 列表里翻。

### 归档删除 → 文件夹管理（已拍板）

归档和"不给它发消息"没有区别（agent 本来就只在被消息触发时才唤醒）。archived 状态
彻底删除，`agents` 表加 `grp TEXT DEFAULT ''`：agent 列表按文件夹分组折叠显示，组名
随起随用。迁移：现有 archived agent → 文件夹"归档"、status 恢复 active。
paused（连续失败自动暂停）保留。

### 列表 UI/UX（v4.1，2026-09-18 追加）

- **Ctrl+Enter 换行**（Enter 发送不变，Shift+Enter 保留）——已随 v4.0 提前落地；
- **左侧栏拖边框调宽**：竖向拖拽把手，宽度记 localStorage；
- **agent 长按拖动排序**：组内拖动改上下顺序（`sort_order` 列）。与 v3.4 的"最近唤醒
  浮顶"的调和规则：**默认自动排（last_wake_at 倒序），某组一旦手动拖过即锁定手动序**，
  组菜单一键"恢复自动排序"。

---

## 6. 对话树（v4.4，压轴大件）

### 数据模型

```sql
CREATE TABLE tree_nodes(
  id INTEGER PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  parent_id INTEGER,            -- NULL = 挂在该 agent 的虚根（Dummy）下
  label TEXT NOT NULL,          -- 自动递增数字，可双击改名（0 token）
  session_id TEXT,              -- 该节点收工时的 claude 会话 id（fork 出的）
  kind TEXT DEFAULT 'wake',     -- wake | compact
  created_at REAL, convs TEXT   -- 本轮涉及的会话 id 列表（回撤提示用）
);
-- agents 加 head_node_id；messages 加 node_id（该消息属于哪次唤醒轮）
```

### 机制

- **节点粒度 = 一次唤醒**（≈ 你的每条消息一个节点，已与 John 确认是同一概念）：
  仅两种合并情形——1.5s 内连发的消息攒成一批算一轮；干活途中捎带送达的（btw）归
  当前节点。原因是 fork 只能发生在进程收工的轮边界；
- 每次唤醒：`--resume <head.session_id> --fork-session` → 从事件流读回新 session_id
  → 建子节点、head 前移。每个节点永远可回切（它的转录文件此后不再被写）；
- 虚根（Dummy）：从根新开对话 = 全新 session（等同再走一次首次唤醒，系统规则重发），
  实现"一开始就能多个平行对话"；
- **回撤**：head 指到任意节点即可；某节点"右移出局" = head 指到它父节点，该节点及
  子孙留在树上随时可看可回切。聊天记录**永不删除**——树改变的只是"agent 记得什么"；
- /compact 也建节点（kind=compact，fork 后压缩），树上用 🧹 图标区别于普通圆点节点
  （John 2026-09-18 提议）。**压缩前的节点照样可选**：fork-per-wake 意味着每个节点的
  转录文件此后不再被写，选压缩前节点 = 恢复完整未压缩记忆，压缩后的分支继续存在，
  两者并行互不影响（代价：回压缩前节点的下一轮要付它那份长上下文的冷唤醒钱——
  它长正是当初压缩的原因）；
- 切分支 0 token；成本账见 §0。

### 按分支切换 model / effort（John 2026-09-18 提议，采纳）

每个树节点记录本轮使用的 model/effort；从任意节点开新分支时可以换模型或 effort，
该分支后续默认沿用。这正好顺应缓存机制拿到 John 想要的效果：

- **缓存按模型分命名空间**：Sonnet 写的缓存 Opus 读不到。所以"半路换模型"必然是
  一次全量重读——这是 API 层面的事实，任何结构都绕不开；
- 但**分支内模型恒定** = 各分支在自己模型的命名空间里持续保温：A 分支一直 Sonnet、
  B 分支一直 Opus，各自续聊都是前缀命中，互相切换不打架（本就是两份独立转录）；
- 唯二的冷成本：某分支 >缓存 TTL（约 1h）没动过后的第一轮；以及在**同一分支**中途
  改 model/effort 的那一轮（改完新前缀重新保温）。effort 与 model 同理——中途改
  effort 也会作废消息缓存，按分支固定就没这个问题；
- UI：分支设置放节点右键/详情里，节点上用小字标注非默认的 model/effort。
  第三方 provider 模型是否支持缓存取决于服务商，机制本身不受影响。

### UI

私聊右侧可折叠竖栏：缩进树 + 当前 head 高亮 + 每节点【回到此处】【改名】。
点节点 → 主聊天区切换为"根→该节点"路径上的消息（只读预览 + 顶部横条"正在查看
分支 N，发消息将从此处继续/回到当前"）。分支过滤靠 messages.node_id ∈ 路径集合
（树上线前的旧消息 node_id 为空，视为所有分支共有的"史前"段）。

### 要点名的复杂处（诚实预告）

1. **agent 的记忆是跨会话的**（群聊+私聊共用一个 session）。回撤会连它对群聊的记忆
   一起撤。树按 agent 建（放私聊侧栏），节点标注该轮涉及哪些会话，回撤涉群时提示一句。
   群聊时间线本身是共享事实，不参与树、不会被改写。
2. **磁盘**：每次唤醒 fork 一份转录 jsonl（几百 KB～几 MB），长期累积。设置里加
   "清理不被任何节点引用的转录文件"，并可选保留最近 N 天。
3. 旧 agent 首次进入树世界：现 session 作为根下第一个节点，历史消息归"史前"段。

---

## 7. 分期与交付

| 期 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| v4.0 | 拆骨架 + 状态机（零行为变化） | — | 最高（动所有文件），靠回归验证兜底 |
| v4.1 | 打断语义 + 分组去归档 | 4.0 状态机 | 低 |
| v4.2 | 记忆简化、资源库下线 | 独立 | 低（只删不改核心） |
| v4.3 | effort + 模型接入 UI | 独立 | 低 |
| v4.4 | 对话树 | 4.0 runner/treestore | 中（新概念多，UI 工作量大） |

每期一个 commit、可用即交付、单独可回滚。服务端变更照旧走"等全员空闲"的分离看门重启。

## 8. 决策记录（2026-09-18 John 已全部拍板）

1. 技能库：**与 CC 共享 ~/.claude/skills**，调用/更新方案与 CC 一致（不是删功能，是不再自建）。
2. 树节点粒度：**每次唤醒一个节点**（≈ 每条用户消息一个，连发攒批/btw 合并）。
3. 归档：**功能整个去掉**，改文件夹管理；存量归档 agent 迁入"归档"文件夹。
4. held 解除条件：**任何新消息**（含群聊 @）都唤醒，唤醒词带被打断标注（默认采纳，John 未反对）。
5. 追加需求：Ctrl+Enter 换行（v4.0 已落地）、左栏拖宽、agent 长按拖序（→v4.1）、
   压缩节点用 🧹 图标、**按分支切换 model/effort**、可选压缩前节点（→v4.4，见 §6）。
