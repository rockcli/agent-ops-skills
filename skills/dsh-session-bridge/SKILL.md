---
name: dsh-session-bridge
description: 跨宿主定位、读写和派发 DeepSeek Harness（dsh）既有对话（dsh-session.mjs）：按标题/别名查找会话、读历史、compact digest、zstd 全文搜索、发消息（queue/steer）、识别并代答 ask_user_question、管理工作区归属与会话交接。独立只读代码/方案盲审已拆至 dsh-review 技能。
---

# DSH Session Bridge

> **使用边界**：本技能专注于**操作已有 DSH 对话**（读取/续接既有 DSH 会话、跨会话搜索/交接、向已有会话派发写任务）。**独立只读代码/方案审查**（冻结快照、双路缺席补位或按需第三路盲审）已统一独立至 [`dsh-review`](../dsh-review/SKILL.md)（`scripts/dsh.py`，与 `codex-review`、`opus-review` 统一 Python CLI 契约）。

先判断是否需要跨宿主操作已有 DSH 会话。当前宿主内部能原生委派的任务，直接使用原生子代理与完成通知，不走桥接 shell、观察脚本或定时器。

让外部 agent（Codex、Claude Code 等）按**名字/别名**找到用户正在用的 dsh 对话，读内容、发消息、看进度、交接上下文。

## 一句话原理（实测）

dsh web 在 `127.0.0.1:3080` 上跑着一套内部 Remote API（`POST /api/<namespace>/<method>`）。
脚本读取 `$DSH_HOME/.credentials.yaml` 里的 `client-connection/browser-session` 密钥，**自己签出宿主认可的浏览器会话 cookie**，以“本机浏览器”的身份调用：

| 用途 | 端点 |
|---|---|
| 列出全部对话 | `session/list` |
| 读历史 | `session/page` |
| 发消息（冷会话自动 resume） | `session/prompt` |
| 改名 / 新建 / 归档 | `session/rename` · `session/create` · `workspace/archiveSession` |
| 工作区归属（决定 web 侧边栏可见） | `workspace/create` · `workspace.json` |

不需要浏览器自动化、不需要额外 daemon、不需要给 dsh 装插件。

## 前置条件（必须）

1. `dsh web` 正在运行（默认 `http://127.0.0.1:3080`）。
2. 脚本能读到该进程对应的 `$DSH_HOME/.credentials.yaml`（默认 `~/.dsh`）。
   - home 不同加 `--dsh-home`；端口不同加 `--url`。
3. 全文搜索需要 `zstd` 命令（`~/.dsh/sessions/**.zstd` 是多帧压缩）；没有 zstd 时自动降级为只扫最近窗口。

脚本：`~/.agents/skills/dsh-session-bridge/scripts/dsh-session.mjs`（Node 18+，无第三方依赖）

## 命令速查

```bash
S=~/.agents/skills/dsh-session-bridge/scripts/dsh-session.mjs

# 定位
node $S list [--query 关键词] [--all] [--limit N]      # --all 含已归档(×)
node $S resolve "排查 API 超时问题" --json     # 名字 → sessionId
node $S alias set api-debug "排查 API 超时问题"    # 存稳定别名（标题改名也不丢）
node $S alias list | node $S alias remove api-debug
node $S rename api-debug "API 超时排查"                  # 给对话改名

# 读
node $S read  api-debug --limit 40 [--all] [--tools] [--after-seq N]
node $S digest api-debug --chars 400                      # 紧凑交接包（省 token）
node $S questions api-debug                               # 看它有没有在等你回答问题
node $S search "回程黑洞" --limit 5                    # 跨所有对话全文搜索
node $S search "proxy-bench" --days 7 --cwd ~/proj

# 写
node $S send  api-debug "帮我看下现在的排查进展" --wait --heartbeat 15 --json
node $S send  api-debug "补充：只看超时接口" --mode queue          # 排队到下一回合（默认）
node $S send  api-debug "立刻改：先查数据库连接池" --mode steer    # 引导当前回合下一步生效
node $S send  api-debug "继续" --dedupe-id task-42             # 重试不重复投递
node $S queue list api-debug                                    # 看排队/引导中的消息 + ITEM_ID
node $S queue steer api-debug <ITEM_ID>                         # 把排队消息提升为立刻引导
node $S queue remove api-debug <ITEM_ID>                        # 撤回一条排队消息
node $S cancel api-debug                                        # 停当前回合（队列保留）
node $S model                                               # 看当前可选模型/思考强度
node $S model api-debug deepseek-flash max                       # 固定使用 DeepSeek Flash + max（下一 step 生效）
node $S answer api-debug --select target=staging                # controller 代答它的问题
node $S send  api-debug "重构 xxx" --contract                   # 派活时声明 controller 身份，减少无谓提问
node $S watch api-debug --after-seq <发前seq> --interval 2      # 实时流式看进度
node $S handoff api-debug other-chat --message "接手排查" --wait

# 新建 / 归属（worker 场景关键）
node $S new --cwd . --title "节点巡检" --message "先跑一遍连通性"   # 已注册目录自动挂，否则默认隐藏
node $S new --cwd . --create-workspace --title "新项目"              # 显式注册并让侧边栏可见
node $S new --cwd . --no-workspace --title "一次性任务"              # 强制隐藏
node $S adopt <sessionId>        # 把已有的“孤儿”会话挂进工作区，让它出现在 web 侧边栏
```

- `TARGET` = 名字（模糊匹配标题）· sessionId（可只写后半段）· 别名 · `latest`。
- `send --json` 返回 `{sessionId,title,sent,mode,requestId,deduped,delivered,seq,complete,reply,replySeq}`。
- 模型设置命令只接受 `deepseek-flash` + `max`，其他模型或思考强度会在请求 DSH 前被拒绝。
- `send`、`answer`、`handoff` 和 `new --message` 每次调用 `session/prompt` 前，都会重新选择并验证 `deepseek-official/deepseek-flash` + `max`。这样旧会话即使保存过 Pro 路由，桥接发送也不会沿用它；若 Flash 路由不可用，会在投递消息前失败。

## 侧边栏可见性与 workspace 膨胀（已根治）

**根因**：`session/create` 只传 `cwd` 不会出现在 web 侧边栏；此前 `new` 为了让会话可见，会为每个不同的 cwd 自动 `workspace/create`，于是每个临时 worktree、审查快照目录都会在侧边栏留下一个文件夹。

**现行策略**：
1. `new` 先 `realpath(--cwd)`，只挂到**已注册**的 workspace；找不到时默认用 `{ cwd }` 隐藏创建，不新建 workspace。
2. 确实需要新目录出现在侧边栏时，显式加 `--create-workspace`（注册一次后，同目录后续 `new` 自动挂上）。
3. `--no-workspace` 始终强制隐藏；`adopt <sessionId>` 用于事后把已有会话挂进 workspace（不存在时创建）。

> 这样外部 agent 仍能正常读写会话，但不会再把一次性目录写进侧边栏。已有孤儿会话用 `adopt <sessionId|name>` 挂进工作区（内部用 `session/create {workspaceId, sessionId}`，已验证）。

> 注意：会话记录的 cwd 必须是规范路径（`/tmp` vs `/private/tmp` 这类不一致会导致 attach 失败并报错）。

## 长任务“很久没输出”怎么办？

根因是外部 agent 只能看到 `turn/end`，中间过程不可见，于是它倾向把任务拆碎。三个改进：

1. **`watch`**：轮询增量事件并实时打印 `[#seq user/assistant]`、`[tool-call …]`，空闲时打心跳
   `[watch] 20s · running · steps 8 · last #123 tool-call`，回合结束自动退出。
2. **`send --wait --heartbeat 15`**：等待回复期间每 15s 往 stderr 打一条进度心跳。
3. **`digest` 代替“拆任务”**：一个长任务照旧下发，需要中间交接时用 `digest`/`handoff` 取紧凑上下文，而不是把任务切碎。

推荐 worker 协议：`new` 建会话 → `send --wait --heartbeat 15`；中途用 `watch` 观察；重试用 `--dedupe-id`；需要换手用 `handoff`。

## 独立只读审查（已迁至 `dsh-review`）

独立只读代码审查（`start`）与只读方案审查（`task`）已独立为统一 Python 技能 [`dsh-review`](../dsh-review/SKILL.md)（`~/.agents/skills/dsh-review/scripts/dsh.py`），原 `scripts/dsh-review.mjs` 仅保留作为向后兼容转发包装器。

## 增量消息：排队 还是 立刻引导？

DSH 的 `session/prompt` 有两个投递位（实测确认）：

- **`mode: "queue"`（默认）→ `next-turn`**：排队到**当前回合结束后**的下一回合，FIFO。
- **`mode: "steer"` → `next-step`**：在**当前回合的下一个 step 边界**注入，用来纠偏/补约束。

| 场景 | 用什么 | 效果 |
|---|---|---|
| 目标**空闲** | 随便（默认 queue） | 直接开一个新回合（此时两者等价） |
| 目标**正在跑**，追加任务、等它做完这轮 | `send TARGET "..."` | 排到下一回合，按顺序执行 |
| 目标**正在跑**，要马上纠偏/加约束/让它插一句 | `send TARGET "..." --mode steer`（或 `--steer`） | 当前回合下一步生效，**不打断**正在执行的 step |
| 已排队，想改成立刻引导 | `queue list` 取 `ITEM_ID` → `queue steer TARGET ITEM_ID` | 从 `next-turn` 提升到 `next-step` |
| 想撤回一条排队消息 | `queue remove TARGET ITEM_ID` | 移除 |
| 想停掉当前回合 | `cancel TARGET` | 停当前 turn，**已排队消息保留**（DSH 内部 `keepInbox: true`） |

关键澄清（避免 Claude Code 用错）：
- **steer ≠ 打断/abort**。它不中断当前正在执行的 step，只在下一步边界注入；真要停下用 `cancel`。
- 目标空闲时 queue/steer 等价。
- `send` 的 JSON 会返回 `mode`、`targetRunning`、`placement`（`next-turn`/`next-step`）和一句中文 `hint`，据此判断是否要用 steer。
- `queue list` 的 `id` 就是 `session/updateQueue` 需要的 `itemId`。

## 当 dsh 反过来提问（ask_user_question）—— 能识别，能抛出

DSH 的 agent 可以调用 `ask_user_question` 阻塞式提问。实测它在**持久日志里完全可见**：

- `assistant/message` 里的 `tool-call name=ask_user_question` + 一条 `tool/call` 事件，`arguments` 就是完整的 `{questions:[{id,question,header,options,…}]}`；
- 只要**没有对应的 `tool/result`**（或 `turn/end`），就说明它正卡在等你回答，此时 `session/list` 的 `running = true`。

（`user-questions/request` 请求本身是走 Web 客户端的 answerer，外部进程拿不到；但持久日志足以判断“有人在等回答”。）

**识别 / 抛出**（Claude Code、Codex 都能接住）：

```bash
node $S questions api-debug --json      # 列出提问：pending 数组 + 每个问题/选项；有待答时退出码 6
node $S send api-debug "..." --wait     # 若对方中途提问，立即返回 {blocked:true, question:{...}}，退出码 6（不再傻等到超时）
node $S wait api-debug --after-seq N    # 同上
node $S watch api-debug --after-seq N   # 打印 [QUESTION] 横幅并以 6 退出
node $S digest api-debug                # 交接包里带 `- ⚠️ 正在等待回答的问题`
```

**回答它**（两条路）：

1. **人在 dsh web 界面回答**（最正统）：把问题抛给用户，让他在该会话里点选/输入，dsh 继续，随后外部 `wait` 就能拿到结果。
2. **外部代答（已验证的兜底）**：`cancel` 掉那个阻塞回合，再 `send` 把答案作为普通消息发过去。实测可行，但注意语义差别——`ask_user_question` 会被记为 `Error: … aborted before the user answered`，答案是以“用户消息”形式被采纳的，不是正式提交选项。发给模型时最好写清“回答你刚才的问题：X”。

> 不要在提问挂起时干等：`wait` 现在会立刻返回 blocked（退出码 6），`watch` 会在横幅后退出，避免外部 agent 误判成“卡死”。

## Controller 模式：代答 vs 升级给用户

典型场景：**Claude Code / Codex 当 controller，dsh 当 worker**。worker 的提问由 controller 先判断：

- **有信心 → controller 直接代答**，不要打扰用户。
- **业务类决策 → 升级给用户**，附上题目、选项和你的推荐。

### 判断清单

| 直接代答（技术/实现） | 升级用户（业务/风险） |
|---|---|
| 环境、路径、命名、分支 | 产品行为、对外可见变化 |
| 工具/库/命令选择 | 范围与优先级取舍 |
| 重试、超时、并发、日志格式 | 花钱、配额、资源规模 |
| 测试策略、代码风格、常规约定 | 不可逆操作：删数据、发布、推送、对外发送 |
| 只读排查、可回滚的小改动 | 合规/隐私、第三方沟通、审美偏好 |

拿不准时**默认升级**——尤其涉及不可逆动作。宁可多问一句，不要替用户拍板。

### 代答怎么发

```bash
node $S questions api-debug --json                 # 先取题目：question.id + options + 推荐项
node $S answer api-debug --select target=staging   # 结构化代答（会匹配到选项 label）
node $S answer api-debug "staging"                 # 单问题时自由文本，自动匹配选项
node $S answer api-debug --select a=x --select b=y # 多问题批量
node $S answer api-debug "..." --wait --json       # 代答后等它继续的回复
```

**机制说明（重要）**：`ask_user_question` 的正式提交通道只对 Web 客户端开放，所以 `answer` 的做法是
**先 `cancel` 掉阻塞回合，再把答案作为一条带 `[controller 代答]` 前缀的用户消息发过去**（实测 dsh 会据此继续）。
副作用是那次工具调用会记为 `aborted`，答案不是“勾选项”而是用户消息——对 worker 的实际推进没有影响。

### 升级给用户时怎么呈现

从 `questions --json` 拿到结构，转述成：**哪个 dsh 会话 + 它在问什么 + 选项（含它的推荐）+ 你为什么不能替它决定**。用户回答后，再用 `answer` 发回去。

### 减少无谓提问

在派活时加 `--contract`，任务开头会声明 controller 身份并要求：技术问题自行判断、只对业务决策提问、提问必须带推荐项。

```bash
node $S send api-debug "重构 xxx 模块" --contract --wait --heartbeat 15
```

## 名字 → 对话 的解析规则

1. 先查**别名**（精准命中），再 `latest`，再按标题/ID 打分（精确 > 大小写 > ID > 子串 > 字符相似度）。
2. 同分优先：当前目录一致 > 运行中 > 未归档 > 最近更新（归档降权 120）。
3. 找不到（<400 分）或疑似歧义（两个高分差 <50）：退出码 `1`/`3` 并列出候选；`--json` 返回 `{ok:false,error,candidates}`。
   **不要瞎猜**：把候选给用户确认，或直接用 sessionId。

## 使用纪律（重要）

- **发消息是写操作**，真的进入对方 dsh 对话并触发它干活/消耗额度。
  - 用户明确说“发/告诉它/让它继续/问它/把它当 worker 跑” → 直接发。
  - 用户只说“看看/读一下” → 只读。
  - 拿不准内容 → 先给用户确认。
- `read` 默认只给**真人消息 + 助手文本**（注入上下文隐藏，`--json` 带 `injected` 标记）。
- 控制体积：优先 `digest` / `--limit` / `--after-seq`，别一上来 `--all`。
- 默认 `send` 会做**送达校验**（回读确认 user 消息落库）；失败退出码 `5`，可用同一 `--dedupe-id` 安全重试。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `cannot reach dsh web at …` | `dsh web` 没跑或端口不同；`--url` 指定 |
| `authentication rejected … (401)` | `--dsh-home` 指错（读到别的 `.credentials.yaml`） |
| `no client-connection/browser-session secret` | 该 home 没启动过 web；先启动一次 |
| `session/prompt failed: session/model-unavailable` | 桥接会在投递前选择 Flash max；若仍失败，检查 `deepseek-official/deepseek-flash` 是否在运行中 DSH 的可用目录里 |
| 退出码 3 | 名字歧义，换更完整标题 / sessionId / 别名 |
| 退出码 4 | wait 超时；继续 `wait --after-seq` 或 `watch` |
| 退出码 5 | 送达未确认；用同一 `--dedupe-id` 重试 |
| 退出码 6 | 对方卡在 `ask_user_question` 等你回答；用 `questions` 看题目，人在 web 答或 `cancel`+`send` 代答 |
| 搜索很慢/无结果 | 确认 `zstd` 可用；`--days/--cwd` 缩小范围 |

## 实测事实卡

- **投递位实测**：`mode:"queue"` 的消息出现在 `session/list` 的 `inbox.next-turn`；`mode:"steer"` 出现在 `inbox.next-step`。也就是 queue=下一回合、steer=当前回合下一步。
- **steer 不是 abort**：`agent.steer` 只插到 `next-step`，不中断当前 step；`session/cancel` 才是停当前 turn，而且它 `keepInbox: true` —— **取消会保留排队消息**（实测 cancel 后 steering 项仍在）。
- **排队可改**：`session/updateQueue` 支持 `{kind:"edit"|"remove"|"steer"}`；`steer` 把 `next-turn` 项提升进 `next-step`（实测两数组长度随之变化）。
- 发送后对方**立刻**收到 user 消息，冷会话会先自动 resume；`send --wait` 用 requestId 锚定本次消息，不会把正在跑的旧回合当答案。
- `session/prompt` 用 requestId **幂等**：同 `--dedupe-id` 重发不会产生第二个回合（实测 `deduped:true`）。
- 只传 `cwd` 建会话 = 不进 workspace = web 侧边栏看不到；传 `workspaceId` 才可见。`new` 现在默认只挂已注册 workspace，`--create-workspace` 才新建；`dsh-review` 默认隐藏。
- **别名在标题改名后仍然命中**（实测 rename 后 `resolve api-debug` 依旧指向同一 sessionId）。
- `session/search` 端点在本部署被禁用（`openAt: never`），所以 `search` 走本地 zstd 扫描：全量 ~160 个会话约 2–4s。
- **ask_user_question 可识别**：`tool/call name=ask_user_question`（含完整 options）在持久日志里；无匹配 `tool/result` 即“正在等回答”，`running=true`。`send --wait`/`wait`/`watch` 遇到它会返回/打印 blocked 并退出码 6，不会干等超时。
- **外部代答是“abort + 用户消息”**：`cancel` 后工具记为 `Error: ask_user_question was aborted before the user answered`，随后 `send` 的答案以普通 user 消息被采纳（实测 dsh 能据此继续）。
- `answer TARGET --select id=label` 已实测：先 cancel 阻塞回合 → 发送带 `[controller 代答]` 前缀的答案（选项值会自动匹配到完整 option label）→ `--wait` 拿到 worker 继续后的回复；worker 回复里明确“已收到 controller 的回答并确认”。
- `send --contract` 会在任务前插入 controller 声明，要求 worker 技术问题自行判断、只对业务决策提问。
- 共享技能放 `~/.agents/skills`；本机 dsh 默认扫描此目录，当前版本监听技能文件变化，在后续 step 更新目录并在加载时读取内容。Codex 的发现以当前 skills/list 为准；宿主未刷新目录时可以直接读取绝对 SKILL.md 或调用脚本，无需重启 dsh 服务。
