---
name: opus-review
description: 调用 Claude Opus（默认 opus、medium 思考强度；复杂任务可显式传 high）发起独立审查，管理已有外部审查任务。常规审查默认与 codex-review 双路并行独立发起（不预设主控立场）；任一方不可用时由 dsh-review 补位保持双路审查；复杂或主控信心不足时可加开 dsh-review 作为第三路审查；实施修改用 opus-task。
---

# Opus 与双路独立审查

## 默认审查原则（必遵）

1. **默认 Opus + Codex 双路并行发起**：
   - 当用户说“审查”（代码审查、方案审查、收口审查等）且未限定只用单一模型时，**默认将本技能（Opus）与 [`codex-review`](../codex-review/SKILL.md)（使用其默认 Codex 配置）一起并行发起**。
2. **独立审查，严禁预设主控立场**：
   - 传给各审查路的 `prompt` **只准提供客观背景、变更范围（`base..head` 或 working-tree）、用户原始诉求、相关文件路径与项目阶段约束**。
   - **严禁在审查 prompt 中预设主控的立场、塞入倾向性结论、替当前改动辩护或引导审查方向**，确保各方真正独立盲审。
3. **Opus 默认配置（唯一事实源）**：
   - `scripts/opus.py` 默认 **`--model opus --effort medium`**（常规审查与 Codex 对齐；复杂架构或高风险边界可显式传 `--effort high`）；用户当次明确指定其他模型或思考强度时以用户指定为准。
   - Codex 默认配置由 [`codex-review`](../codex-review/SKILL.md) 维护。
4. **不可用时自动补位（尽量保持双路审）**：
   - 默认 Opus + Codex 双路并行时，若任一方不可用或执行失败（如原生子代理或 CLI 未就绪、认证失效、配额/限流、连接故障或超时失败），保留原错误记录后，**默认自动启用 [`dsh-review`](../dsh-review/SKILL.md)（`scripts/dsh.py start` / `task`）补位，尽量保持双路独立审查**，并在汇报中如实说明补位原因。
   - 若用户明确只指定了单一模型且不可用（如单指 Opus 或单指 Codex），则自动切换到另一可用通道（Codex / Opus / DSH）继续完成审查。
5. **可选第三路审查（DSH，由 Controller 按需判定）**：
   - 常规改动默认只跑 Opus + Codex 双路；**当任务足够复杂、涉及高风险跨层边界、双路意见存在关键分歧，或主控（Controller）对结论信心不足时，由主控自主决定启用 [`dsh-review`](../dsh-review/SKILL.md)（`scripts/dsh.py start` / `task`）作为第三路独立盲审**。
6. **外审收敛与防过度打磨（禁止多轮追修合成边界 P2）**：
   - 外审模型天生靠“挑刺”体现价值；外审用于拦截真实阻断缺陷，而非追求“外部模型零意见”。
   - **小型增量快速通道**：对话内已与用户逐项拍板且 diff 新增+删除 < 50 行的增量补丁（**不涉及网络防护、隔离边界、凭据或门禁规则本身**），跳过外部方案预审，最多做 1 轮代码 diff 审查（可与全量门禁/打包准备并行推进）。若非阻断性审查建议会让改动规模膨胀 >= 3 倍或引入复杂新机制，先守住原定范围或请用户确认，不直接扩写。
   - 严格只修**真挂测试、核心故障/主流程阻断、鉴权与安全隔离、数据边界及客户数据完整性红线（实质 P0/P1）**；当上述均已闭环时，**立即收口交付**：
     - 对理论并发/超时竞态、死代码/分支洁癖、极低概率异常、极端多故障叠加恢复等 P2/Low（或无实际可达阻断/越权风险的完备性意见），默认在汇报中记一句**「演示期不修」**直接跳过，**严禁为此叠加复杂补偿状态机、在重编译/重型 `pre-commit` 仓库中顺手改码返工，或开启第 3+ 轮外审循环追修**。
     - 若在轻量仓库中确需顺手做极小无侵入微调，跑通本地最小相关测试后直接收口，绝不再重开外审。

## 宿主判断与执行方式

先检查当前宿主的原生委派能力。**Claude Code 内部**委派 Opus（`medium`）时，优先使用原生子代理及其完成通知（同时通过 [`codex-review`](../codex-review/SKILL.md) 并行发起 Codex 审查）；若原生子代理或外部 CLI 任一方不可用，双路中由 [`dsh-review`](../dsh-review/SKILL.md) 自动补位保持双路（仅单跑时切至可用通道）继续完成审查。
跨宿主调用 Claude/Opus（如在 Codex 或 Antigravity 中）、要求独立 Claude CLI，或正在查询/取消已有外部 jobId 时，使用下面的外部任务流程。

Codex 可使用 `opus-review` MCP 工具或下方 CLI；Antigravity 直接使用下方 CLI 启动同一 Claude 进程，没有模型转发子代理。
工具暂未加载时，直接运行本技能目录的 `scripts/opus.py`，两者使用相同任务记录。
默认 Claude `opus`、`medium` effort；用户指定的模型、effort 和审查范围优先。

## 发起与续审

- 代码：调用 `review_start(repo, base?, head?, scope?, prompt?, model="opus", effort="medium", owner_thread?)` 或 CLI `start`。
  `repo` 是绝对路径；指定 `base` 时默认 branch，否则默认 working-tree。
  工具冻结 base/head SHA 和源码快照，包含未忽略的未跟踪文件；提交差异是 base 到 head，不自动改用 merge-base。
  “未 push 的代码”应先查 upstream 与本地 HEAD，再传入核实过的 base/head。
  **注意：凡涉及已有代码变更（`base..head` 或 working-tree diff）的审查，一律调用 `start`（由脚本在进程外安全生成 `context/diff.patch` 与 `manifest.json`），严禁用无 `diff.patch` 且无 `Bash` 的只读 `task` 审查代码变更。**
- 方案或不要求冻结 Git 差异的只读调查：使用 `task_start(repo, prompt, write=false, model="opus", effort="medium", ...)` 或 CLI `task`（同样默认与 `codex-task` 并行独立发起，不预设主控立场）。
- 普通审查与挑战性审查共用入口，按用户要求组织客观中立的 `prompt`，不预设主控立场，不为额外关注点另加转发层。
- 续审：传 `resume_job` 和新问题；原任务须已结束，工具复用原 Claude session 和原冻结快照。
  代码已经更新时，启动新审查，明确新范围，不把旧快照续审当成新代码验收。
- 保存返回的 `jobId`。后台启动后可继续其他工作；需要等结果时查询该任务，不重新发起审查。

审查进程仅有 Read/Glob/Grep，禁用自定义技能、hooks 和继承 MCP。
`scripts/opus.py` 内置 Fail-Closed 本地代理门禁（自动继承环境变量或 macOS `scutil --proxy` 并做端口探活，代理缺失、被 `NO_PROXY` 绕过或不可达时拒绝启动 Claude，严禁退化为直连）。
只读工具限制不等于操作系统沙箱；不要为了读取缺失证据放开 Bash 或写入能力。
快照不含完整 Git 历史或未单独展开的 submodule，必要时由主模型在授权范围内补充证据。发起新 `review` 时，`scripts/opus.py` 会自动清理同 `stateDir/jobs` 下已终态（`completed`/`failed`/`cancelled`）且超过 3 天未使用的历史 `source/` 源码快照（保留 `job.json`、`prompt.txt`、`final.txt`、`context/` 审查结论与证据，且不删除任何活跃任务或续审仍在引用的目录）。

## 状态与结果

- `review_status(job_id)`：查状态；省略 ID 可列出任务，尽量用 repo 缩小范围。
- `review_result(job_id)`：读取保存的完整结果，不会再次调用模型。
- `review_cancel(job_id)`：仅在用户要求停止该任务时取消；通过 PID 身份核对后终止进程组。
- `review_setup()`：只读检查 CLI 和认证，不打印邮箱、令牌或原始认证响应。

`completed` 表示模型执行完成，是否通过取决于实际发现；`needs_input` 表示发生权限拒绝、覆盖不完整。
`failed`、超时、丢失 worker 或空结果都不能称为审查通过。双路审查中若 Opus 不可用或执行失败，默认启用 [`dsh-review`](../dsh-review/SKILL.md) 补位以尽量保持双路（仅单跑 Opus 时切换至 [`codex-review`](../codex-review/SKILL.md) 或 [`dsh-review`](../dsh-review/SKILL.md)）并说明原因。
报告具体问题、代码位置、审查范围与缺口；区分请求模型、CLI 报告模型与未知信息，不把 alias 当作真实模型证据。
不要自动执行评审中的修复；根据当前任务已有授权判断后续工作。

## 后台完成通知

用户要求持续跟进、完成后通知或及时发现终态时，发起后台任务后**同回合接上原聊天完成通知**，
不要只留下“稍后查状态”，也不要创建 GPT 转发子代理。
读取 [完成通知接入](references/monitoring.md)：优先使用宿主原生后台任务完成通知；宿主不支持、观察进程丢失或需要持久恢复时，才使用原聊天定时兜底。
原生回调与兜底共用同一 jobId；通知前原子 claim，持 token ack，运行中静默，不重复通知。

## CLI 备用入口

下面的 `<skill-root>` 取本 SKILL.md 所在目录。调用时将其解析为绝对路径，不依赖当前工作目录。

```text
# Antigravity 快捷模板（其他宿主请替换为本技能根目录；默认 state: $HOME/.codex/state/opus-review；--state-dir 可放子命令前或后）
# 1) 代码 diff 审查：start（有 base 时传 --base BASE --head HEAD；工作区 diff 传 --scope working-tree）
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" start --repo /abs/repo --base BASE --head HEAD --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
# 2) 只读方案/调查：task（不带 --write）
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" task --repo /abs/repo --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
# 3) 后台观察（WaitMsBeforeAsync: 500）+ 原生任务绑定 + 终态原子领取与确认
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" watch JOB_ID --timeout 1800
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" bind-native JOB_ID claude HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" claim JOB_ID --source native:HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" ack JOB_ID --claim-token TOKEN
# 常用管理：status / result / cancel / setup
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" status [JOB_ID]
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" result JOB_ID
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" cancel JOB_ID
python3 "$HOME/.gemini/config/skills/opus-review/scripts/opus.py" setup
```

复杂请求通过 UTF-8 prompt 文件或结构化 MCP 参数传递，不拼入 shell 字符串。
通知领取可用 `review_claim(job_id, source)` 或 CLI `claim JOB_ID --source SOURCE`；只有 notify=true 的领取者可以报告，随后 `ack JOB_ID --claim-token TOKEN`。
状态位于 `~/.codex/state/opus-review`，可跨回合查询；不会在聊天结束时自动杀死任务或删除快照。
开源来源和本地补齐范围见 [来源记录](references/provenance.md)。
