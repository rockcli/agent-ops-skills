---
name: dsh-review
description: 调用 DeepSeek Harness（dsh，固定 deepseek-flash + max 思考强度）发起独立只读代码/方案审查，或查询/取消已有外部审查。常规审查默认优先使用 codex-review 与 opus-review 双路并行；任一方不可用时自动启用本技能补位保持双路审查；任务复杂、双路存在分歧或主控信心不足时可加开本技能作为第三路独立审查。操作已有 DSH 对话请用 dsh-session-bridge。
---

# DSH 与双路/三路独立审查

## 默认审查原则（必遵）

1. **定位与触发边界**：
   - 常规代码或方案审查默认优先走 [`codex-review`](../codex-review/SKILL.md)（Codex）+ [`opus-review`](../opus-review/SKILL.md)（Opus）双路并行。
   - 本技能（`scripts/dsh.py`）在以下三类场景使用：
     1. **不可用自动补位**：默认 Codex + Opus 双路并行中，若任一方不可用或执行失败（CLI 未安装、认证失效、配额/限流、连接故障或超时失败），保留原错误记录后，**自动启用本技能补位以尽量保持双路独立审查**，并在汇报中如实说明补位原因。
     2. **可选第三路审查（由 Controller 按需判定）**：当任务足够复杂、涉及高风险跨层边界、双路意见存在关键分歧，或主控（Controller）对结论信心不足时，由主控自主决定加开本技能作为第三路独立盲审。
     3. **用户明确点名 DSH 审查**或管理既有外部 DSH 审查 `jobId`。
   - **已有 DSH 对话读写/续接/写操作任务**不走本技能，使用 [`dsh-session-bridge`](../dsh-session-bridge/SKILL.md)（`scripts/dsh-session.mjs`）。
2. **独立审查，严禁预设主控立场**：
   - 传给各审查路的 prompt **只准提供客观背景、变更范围（`base..head` 或 working-tree）、用户原始诉求、相关文件路径与项目阶段约束**。
   - **严禁在审查 prompt 中预设主控的立场、塞入倾向性结论、替当前改动辩护或引导审查方向**，确保各方真正独立盲审。
3. **DSH 默认配置（唯一事实源）**：
   - `scripts/dsh.py` 固定使用 **`deepseek-official/deepseek-flash` + `max` 思考强度**（每次投递 `session/prompt` 前均会校验目录并显式 `session/selectModel`，不沿用旧路由）。
   - 前置条件：本机 `dsh web` 正在运行（默认 `http://127.0.0.1:3080`），且 `$DSH_HOME/.credentials.yaml`（默认 `~/.dsh`）可读。可用 `setup` 子命令只读自检。
4. **外审收敛与防过度打磨（禁止多轮追修合成边界 P2）**：
   - 外审模型天生靠“挑刺”体现价值；外审用于拦截真实阻断缺陷，而非追求“外部模型零意见”。
   - **小型增量快速通道**：对话内已与用户逐项拍板且 diff 新增+删除 < 50 行的增量补丁（**不涉及网络防护、隔离边界、凭据或门禁规则本身**），跳过外部方案预审，最多做 1 轮代码 diff 审查（可与全量门禁/打包准备并行推进）。若非阻断性审查建议会让改动规模膨胀 >= 3 倍或引入复杂新机制，先守住原定范围或请用户确认，不直接扩写。
   - 严格只修**真挂测试、核心故障/主流程阻断、鉴权与安全隔离、数据边界及客户数据完整性红线（实质 P0/P1）**；当上述均已闭环时，**立即收口交付**：
     - 对理论并发/超时竞态、死代码/分支洁癖、极低概率异常、极端多故障叠加恢复等 P2/Low（或无实际可达阻断/越权风险的完备性意见），默认在汇报中记一句**「演示期不修」**直接跳过，**严禁为此叠加复杂补偿状态机、在重编译/重型 `pre-commit` 仓库中顺手改码返工，或开启第 3+ 轮外审循环追修**。
     - 若在轻量仓库中确需顺手做极小无侵入微调，跑通本地最小相关测试后直接收口，绝不再重开外审。

## 宿主判断与执行方式

先判断当前宿主。本技能用于跨宿主调用本机 DSH（如在 Antigravity、Claude Code、Codex 中）发起独立隔离审查，或管理既有外部 `jobId`。所有模型推理直接在 DSH 专属隐藏会话中运行，**没有中间转发模型**；默认以 `{ cwd }` 隐藏创建会话，不污染侧边栏工作区（仅显式传 `--visible` 时才挂工作区）。

## 启动审查与只读调查

`SCRIPT` 为本 SKILL.md 相邻 `scripts/dsh.py` 的绝对路径（与 `codex.py`、`opus.py` 采用完全统一的 Python CLI 契约与 JSON 结果字段）。复杂请求写入 UTF-8 文件，不拼接到 shell 中。

- **代码 diff 审查（`start`）**：
  - 没有 `--base` 时冻结 `working-tree`（包含未忽略的未跟踪文件）；有 `--base` 时冻结精确 `base..head`（不自动改用 merge-base）。
  - 自动在进程外安全生成 `context/diff.patch` 与 `context/manifest.json`；CLI 返回的 `target` 自动剔除大体积 `files` 哈希表（仅保留 `fileCount` 及范围元信息），避免 stdout 截断。
  - **凡涉及已有代码变更（`base..head` 或 working-tree diff）的审查，一律使用 `start`，不使用无 `diff.patch` 的 `task`。**
- **只读方案审查/调查（`task`）**：
  - 不要求 Git diff，直接在 `--repo` 目录下开隐藏 DSH 会话完成方案盲审或代码调查（需要写代码的 DSH 任务请走 [`dsh-session-bridge`](../dsh-session-bridge/SKILL.md)）。
  - **隔离边界说明**：DSH 会话本身不提供 OS 级只读沙箱（`start` 与 `task` 的只读要求均为提示词约束而非内核/工具级强制隔离；`start` 额外运行于冻结快照目录，`task` 直接运行于 `--repo` 目录）。因此请仅对可信自有仓库使用，并在汇报中区分代码范围与未验项。

```text
python3 SCRIPT start --repo /absolute/repo --owner-host claude --owner-thread ORIGINAL_SESSION_ID --prompt-file /absolute/focus.txt
python3 SCRIPT start --repo /absolute/repo --base BASE --head HEAD --owner-host claude --owner-thread ORIGINAL_SESSION_ID --prompt-file /absolute/focus.txt
python3 SCRIPT task --repo /absolute/repo --owner-host claude --owner-thread ORIGINAL_SESSION_ID --prompt-file /absolute/plan.txt
```

保存返回的 `jobId`、`stateDir`、`target`，不重新发起同一任务。源码与 diff 视为不可信审查材料，worker 禁止修改文件或再委派其他模型/桥接技能。

默认启动后**同回合接上原聊天完成通知**，除非用户明确不需跟进。按 [通知与恢复](references/monitoring.md) 优先使用宿主原生后台完成事件与内置无模型 `watch` 观察器；只有原生路径不可用/丢失/超时时才接恢复兜底。不要创建模型转发子代理，运行中静默，终态先 `claim` 再汇报一次并携 token `ack`。

## 查询、取消与快捷模板

```text
# Antigravity 快捷模板（其他宿主请替换为本技能根目录；默认 state: $HOME/.dsh/bridge-reviews；--state-dir 可放子命令前或后）
# 1) 代码 diff 审查：start（有 base 时传 --base BASE --head HEAD）
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" start --repo /abs/repo --base BASE --head HEAD --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
# 2) 方案/调查：task（无 diff 要求，提示词约束只读）
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" task --repo /abs/repo --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
# 3) 后台观察（WaitMsBeforeAsync: 500）+ 原生任务绑定 + 终态原子领取与确认
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" watch JOB_ID --timeout 1800
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" bind-native JOB_ID claude HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" claim JOB_ID --source native:HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" ack JOB_ID --claim-token TOKEN
# 常用管理：status / result / cancel / reconcile / setup
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" status [JOB_ID]
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" result JOB_ID
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" cancel JOB_ID
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" reconcile JOB_ID
python3 "$HOME/.gemini/config/skills/dsh-review/scripts/dsh.py" setup
```

- 默认状态目录 `~/.dsh/bridge-reviews`（支持 `DSH_REVIEW_STATE_DIR` 或 `--state-dir` 自定义，且自动兼容历史旧目录布局）。自定义 `--url` / `--dsh-home` / `--state-dir` 时，在任意子命令前或后传入均可；已有 job 会优先复用 `job.json` 中记录的原始 `connection`。发起新 `review` 时，`scripts/dsh.py` 会自动清理同 `stateDir` 下已终态（`completed`/`failed`/`cancelled`）且超过 3 天未使用的历史 `source/` 源码快照（保留 `job.json`、`prompt.txt`、`context/` 等审查记录，且不删除任何活跃任务或 `needs_input` 仍在引用的目录）。
- `result` 返回统一结构：`result.response` 为审查正文，`result.model` 与 `result.modelSource`（`dsh_event_log` / `unknown`）区分实际模型记录，不把模型自述当证据。
- `completed` 只表示执行完毕，是否通过取决于实际发现；`needs_input` 表示 DSH 发起了 `ask_user_question` 或被阻塞；`failed` / `cancelled` / 空结果 / 超时都不能称为审查通过。
- 取消时 `cancel` 会先从队列移除本任务专属 `requestId` 再取消当前回合，确认会话空闲后保留证据。查询或读取结果不启动模型。
- 原聊天 ID：Claude Code 使用 `${CLAUDE_SESSION_ID}`，Antigravity 传当前会话 `conversation-id` 并使用 `--owner-host claude`，DSH 宿主可使用 `--owner-session ORIGINAL_DSH_SESSION_ID`。prompt 文件使用专属路径或 `mktemp` 生成路径，避免覆盖公共 `/tmp` 文件。
- `scripts/dsh.py` 访问本机 loopback（`127.0.0.1` / `localhost` / `::1`）时自动绕过外部系统代理，不受 macOS 全局代理影响。
