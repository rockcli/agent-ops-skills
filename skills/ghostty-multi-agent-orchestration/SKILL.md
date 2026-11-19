---
name: "ghostty-multi-agent-orchestration"
description: "Manage agent processes and panels in Ghostty when the user asks to use Ghostty or references existing Ghostty panels. Preserve sessions and verify process ownership before recovery, continuation, or cleanup."
---

# Ghostty 多 Agent 并行编排

Use this skill when orchestrating multiple AI agents (pi / claude / codex / kimi)
in Ghostty panels, supervising them, or planning multi-branch merges.
普通 review、一般并行或 Codex 任务状态查询不自动选择 Ghostty。Claude 写任务优先使用项目已有 job controller；本 Skill 不绕过其 registry、工作区隔离和授权。

## 0. 环境安全红线（2026-08-12 事故教训）

- **禁止 kill 用户正在使用的 Ghostty 应用**（SIGTERM 也算）。新 surface 全是 👻 空壳、
  命令不执行时，正确处置：只读诊断（ps 查 tty 进程 + test_tab.sh 验证通道）→
  报告用户手动重启。「重启」playbook 只适用于 watcher 对 pi 任务的恢复，
  **不适用于用户环境**。
- kill 后果：macOS 恢复窗口但不恢复进程，用户面板里的 pi/claude 全丢；且自己的
  `open -a` 重启不一定成功——破坏了环境却修不了，最后靠用户手动恢复。
- 开「恢复面板」前必须先确认原实例已死（ps/lsof 查进程与 session 归属），
  否则同一 session 两个 pi 实例双写同一 jsonl。
- 涉及用户环境（终端应用、面板、进程、quit/kill）的操作一律先问；
  `osascript quit` 卡住（确认弹窗）时停下问用户，不要 escalate 到 kill。
- **合并/验收完成后的清理（2026-08-13 规则）**：代码允许合并时，先检查原任务面板
  是否还开着（ps 查 claude/pi 进程 + Ghostty surface），如果在就关闭：
  kill 任务进程 → `close terminal id "<UUID>"` 关 surface →
  `git worktree remove <wt>`（分支保留，已合并）。防止废弃任务面板长期挂着。

## 0.5 派活方式：面板 TUI vs headless（先选方式再动手）

- **默认 headless**（`claude -p "$(cat prompt.md)"`）：
  纯执行/机械/无交互 gate 的任务，零 GUI 依赖、可并行、完成判定干净
  （进程退出 + DONE 标记双信号）；limit 用重试循环 + `--resume` 续跑
- **面板 TUI 只留给**：① 有「停下等用户」gate 的任务；② 用户想围观/干预的任务；
  ③ 长任务跨 limit 需同会话无损续跑
- hybrid：headless 跑 + prompt 约定 `STOP_NEED_CONFIRM` 标记退出 → 人工 `--resume` 续跑
- **pi/codex 同样适用且更该默认 headless**：pi 用 `-p/--mode json/--mode rpc`，
  codex 用 `codex exec`（原生非交互）。它们没有 claude 式 5h limit 弹窗，
  面板对它们只剩围观/干预/gate 价值，无续跑优势。
- **统一监控 watcher 模式**：同时监控 claude + codex 会话状态、写入结构化事件流（如 `/tmp/agent_events.jsonl`）并做 state 持久化。

## 1. 开任务面板（标准流程）

1. **独立 worktree**：`git worktree add -b codex/pi-<任务> <路径> HEAD`
2. **包装脚本**（如 `scripts/run_agent_task.sh` 模式）：cd worktree + exec agent；**写完必须 chmod +x**
3. **新标签页启动**：`make new tab in window 1 with configuration cfg`（报桥接错误但创建成功，忽略）
4. **拿 terminal id**：枚举全部 terminal，与创建前对比取**差集**（新 tab 可能插到任意 index，不能假设在最后）
5. **等就绪再发任务**：agent 启动早期 input text 会被丢；先确认 session 文件出现或发 `hi` 试探
6. **提示词约束**：目标 + worktree 分支不合并 + 高风险/需决策时停下等用户

## 2. 各 CLI 启动姿势（全部踩过坑）

| CLI | 开局方式 | 坑 |
|---|---|---|
| pi | `exec pi --model <provider>/<model> --thinking max`（不带 prompt），新 tab 后 input text 发任务 | 启动早期消息会丢 |
| claude | `exec claude "$(cat prompt.md)"` 直接带 prompt 开局 ✓ | 路径在 `~/.local/bin/claude`（不在 homebrew）；并发易撞 API 断连（ECONNRESET）→ Esc+continue |
| codex | `exec codex "$(cat prompt.md)"` 直接开局 ✓ | 按当前用户授权和 runtime 权限设置运行；不要因避免审批提示自动扩大权限。仍需 worktree 隔离和任务边界 |
| kimi | ❌ 不能带 prompt 开局（`-p` 被 wrapper 吃掉）→ `kimi --auto` 后 input text 发 | `-p` 不能与 `--auto`/`-y` 组合；进程名 `kimi-code`；session 在 `~/.kimi-code/` |

**Headless 授权矩阵（2026-08-13 实测）**：

| Agent | Headless 命令 | 授权 |
|---|---|---|
| claude | `claude -p "<prompt>" --output-format text` | 采用当前已授权的权限模式 |
| codex | `codex exec "<prompt>"` | 采用当前已授权的权限模式 |
| kimi | `kimi -p "<prompt>" --output-format text` | **默认全自动**（实测工具调用自动执行，无需参数；`-p` 与 `--auto`/`-y` 冲突不能组合） |
| pi | `pi -p "<prompt>" --model <provider>/<model> --thinking max` | **默认全自动**（12 个任务全程零授权直接干活） |

- kimi -p 结束会提示恢复命令：`kimi -r session_xxx`（-r 是 -p 模式专属的恢复方式）
- headless 完成判定：输出落盘 + 进程退出（无 TUI 挂着的问题）

## 3. 恢复/捡回会话

- 关闭面板**零损失**：分支/worktree/session 全在磁盘
- 恢复命令：包装脚本 + `pi --session <部分UUID>`（**不是 `--resume`**——那是交互选择器）
- kimi：`kimi -S <session-id>`；claude：`--continue`/`-c`
- 验证恢复：发消息看是否写进**同一个** session 文件（延续 = 成功）

**恢复命令实测矩阵（2026-08-13 三个全部验证通过，标记记忆测试）**：

| Agent | 创建 | 恢复命令 | session id 来源 |
|---|---|---|---|
| claude | `claude -p "..."` | `claude -p --resume <uuid> "继续"` | `~/.claude/projects/<目录转义>/<uuid>.jsonl` 文件名 |
| codex | `codex exec "..."` | `codex exec resume <uuid> "继续"` | `~/.codex/sessions/<年>/<月>/<日>/rollout-*-<uuid>.jsonl` **文件名里的 uuid**（目录是多层日期，别取目录名） |
| kimi | `kimi -p "..."` | `kimi -r session_xxx -p "继续"`（-r 是 -p 专属；面板恢复用 `-S <id>`） | kimi -p 结束时的提示 `To resume this session: kimi -r session_xxx` |
| pi | `pi -p "..." --model ...` | `pi -p --session <部分UUID> "继续" --model ...`（面板同参数新 tab） | `~/.pi/agent/sessions/--<路径转义>--/<时间戳>_<uuid>.jsonl`，**部分 UUID 即可** |

pi 恢复注意（踩过）：`--resume` 是交互选择器不能用；`--session` 用**部分 UUID**（完整文件名会报 No session found）；**cwd 必须匹配原 session 项目目录**（否则提示 fork 确认，headless 下卡住）——先 cd 回原 worktree
- ⚠️ **面板启动异常/进程挂掉时，先找原 session 恢复（claude `--continue`/`-S`，kimi `-S`，pi `--session`），不要重开新对话浪费 token**。实例：claude 面板两次主进程未出现，用户手动 resume 原对话（2026-08-13）
- ⚠️ claude 项目目录转义用**连字符**（`-Users-alice-projects-my-app-`），与 pi 的 `--`+下划线+`--` 不同——拼路径前先 `ls -d ~/.claude/projects/*关键字*` 确认

## 4. 监督监控（watcher 模式，血泪教训）

- **静默分支不能完全静默**：任务既不是"等确认"也不是"完成"时，纯停顿会无人发现。必须：
  - 静默 >10min 非确认非完成 → 通知"停滞"
  - 最后消息含 API Error/Connection lost → 立即通知 + 自动 continue（对 claude 安全）
  - 需求确认 → 通知用户等用户；完成 → 记录；进程退出 → 通知
- **完成判定核对终态输出与实际 evidence**：不能 grep 整个 session 的 DONE（prompt 里也可能有该词）；最后一条 assistant 文本也不能替代 diff、产物、测试和任务验收。
- **mtime、CPU 和进程存活只说明活跃度**：TUI 可能在等待输入、受阻或已经完成。结合当前 session 的终态、工具活动、产物和测试判断；不能把数分钟无更新或 CPU 0% 当作成功。
- **通知与轮询**：runtime 有完成通知时以事件为主，仅设与任务匹配的超时兜底；无通知时使用有界 watcher。单次 timeout 不等于失败或关闭依据。
- DONE 词表要全（"修复完成"/"Done. Summary"等），否则已完成任务被误报停滞
- claude limit 监控：`claude_limit_watcher.py` 单会话版，多实例用 `WATCH_SESSION`/`WATCH_TERMINAL_ID`/`WATCH_STATE`（**WATCH_STATE 必须独立**）
- `server_error`/`overloaded` 不是限流：短等 60s 就 continue，不要等 30 分钟
- 每个自动行为（补位/推送/重启）**独立开关**，禁用要彻底（"关了一处另一处还在开面板"）
- pi session 目录：`--` + 路径(`/`→`-`) + `--`（如 `--Users-alice-projects-my_app--`）；pi 与 claude 的 jsonl 格式不同（`toolCall` vs `tool_use`），解析分开写

## 5. 审查-修复-复审闭环

- **需要复核实际修复证据**：kimi 复查抓到过一次"假修复"——commit message 声称"贪婪匹配到最后 @"，实际 `[^@]*@` 仍匹配第一个 @（正则语义错误），泄漏密码片段。复核故障链和相关断言；是否另派 reviewer 按用户要求及已批准的风险/独立验收规则决定。
- **读 agent 报告要结合操作历史验证，不能只看最后一句**：codex 报告"worktree 保持干净，未产生额外改动"被误读成"没干活"——实际它改了+提交+部署了（"干净"指重跑测试后无脏文件）。判断 agent 是否干活：`git log` / rollout 文件 / session 尾部，三选一交叉验证
- 修复完成的标准：**实测验证**——对真实输入样例跑一遍确认输出（如脱敏逻辑要喂 `user:p@ss@host` 实测），不要只凭直觉/语法检查
- 谁审查出来的问题，修完后**恢复原对话让同一个审查者复审**（保持上下文）
- 新会话复审时附上原报告路径
- 高风险操作扫描模式：git push/merge、docker 容器操作、kill -9、rm -rf、ssh/scp 生产、psql/redis 写、DB 结构变更 → 弹通知 + 记录
- **并行 agent 共享本机服务栈时记录"谁动了环境"**：今天 codex 重启 backend（换 DB_DEMO）、claude 也动过 backend + 建软链 + npm ci——多个 agent 改同一栈会互相踩。派发前记录服务基线（进程 pid/端口/配置来源），agent 改环境必须报告（claude 做到了，要养成习惯）
- **凭据排障三查**：① 代码里的读取链（config/env/dotenv）② shell export 是否遮蔽（zshrc 等）③ **进程实际生效值**（`ps eww -p <pid>`）——改配置不生效时 90% 是遮蔽或读取方式（如 token 含 `=` 不能用 `cut -d=`）
- **复杂任务先写简报文档再派**（/tmp 或项目内）：复杂 workflow 修复和冒烟测试排查都可以靠前置简报省去大量上下文重挖——简报 = 根因 + 关键文件 + 边界 + 验证命令

## 6. 多分支合并推演

- 演练：主仓库建临时分支，按序 `git merge --no-ff --no-commit` 各分支，冲突 `--abort` 记录，最后删——主 HEAD 零污染
- **重叠矩阵用"分支自己的 diff"**：`git diff <分支创建base>..HEAD --name-only` ∩ `git diff <主仓库合入前>..HEAD --name-only`（merge-base 会算错）
- git 对同文件不同区域自动合并，"文件重叠" ≠ "冲突"；真风险是同源工作的重复实现（两边修同一逻辑）
- 验收分工按当前任务、可用能力和独立性决定；不要按模型品牌固定裁决/执行角色，也不要为了消耗额度增加调用。
