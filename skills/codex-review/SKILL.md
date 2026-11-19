---
name: codex-review
description: 调用 Codex（默认 gpt-5.4，可用 CODEX_REVIEW_MODEL 或 --model 覆盖，medium 思考强度、不开 fast）发起独立审查，或查询/取消已有外部审查。常规审查默认与 opus-review 双路并行独立发起（不预设主控立场）；任一方不可用时由 dsh-review 补位保持双路审查；复杂或主控信心不足时可加开 dsh-review 作为第三路审查；实施修改用 codex-task。
---

# Codex 与双路独立审查

## 默认审查原则（必遵）

1. **默认 Codex + Opus 双路并行发起**：
   - 当用户说“审查”（包括代码审查、方案审查、收口审查等）且未限定只用单一模型时，**默认将本技能（Codex）与 [`opus-review`](../opus-review/SKILL.md)（Opus）一起并行发起**，各自使用其运行器默认配置。
2. **独立审查，严禁预设主控立场**：
   - 传给各审查路的 prompt **只准提供客观背景、变更范围（`base..head` 或 working-tree）、用户原始诉求、相关文件路径与项目阶段约束**。
   - **严禁在审查 prompt 中预设主控的立场、塞入倾向性结论、替当前改动辩护或引导审查方向**，确保各方真正独立盲审。
3. **Codex 默认配置（唯一事实源）**：
   - `scripts/codex.py` 内置默认 **`--model $CODEX_REVIEW_MODEL`（默认 `gpt-5.4`）`--effort medium --service-tier default`**（不开 `fast`）；调用方省略 `--model` / `--effort` 即自动生效，用户当次明确指定其他模型、思考强度或 `--service-tier fast` 时以用户指定为准。
   - Opus 默认配置由 [`opus-review`](../opus-review/SKILL.md) 维护。
4. **不可用时自动补位（尽量保持双路审）**：
   - 默认 Codex + Opus 双路并行时，若任一方不可用或执行失败（如 CLI 未安装、认证失效、配额/限流、连接故障或异常中断），保留原错误记录后，**默认自动启用 [`dsh-review`](../dsh-review/SKILL.md)（`scripts/dsh.py start` / `task`）补位，尽量保持双路独立审查**，并在汇报中如实说明补位原因。
   - 若用户明确只指定了单一模型且不可用（如单指 Codex 或单指 Opus），则自动切换到另一可用通道（Opus / Codex / DSH）继续完成审查。
5. **可选第三路审查（DSH，由 Controller 按需判定）**：
   - 常规改动默认只跑 Codex + Opus 双路；**当任务足够复杂、涉及高风险跨层边界、双路意见存在关键分歧，或主控（Controller）对结论信心不足时，由主控自主决定启用 [`dsh-review`](../dsh-review/SKILL.md)（`scripts/dsh.py start` / `task`）作为第三路独立盲审**。
6. **外审收敛与防过度打磨（禁止多轮追修合成边界 P2）**：
   - 外审模型天生靠“挑刺”体现价值；外审用于拦截真实阻断缺陷，而非追求“外部模型零意见”。
   - **小型增量快速通道**：对话内已与用户逐项拍板且 diff 新增+删除 < 50 行的增量补丁（**不涉及网络防护、隔离边界、凭据或门禁规则本身**），跳过外部方案预审，最多做 1 轮代码 diff 审查（可与全量门禁/打包准备并行推进）。若非阻断性审查建议会让改动规模膨胀 >= 3 倍或引入复杂新机制，先守住原定范围或请用户确认，不直接扩写。
   - 严格只修**真挂测试、核心故障/主流程阻断、鉴权与安全隔离、数据边界及客户数据完整性红线（实质 P0/P1）**；当上述均已闭环时，**立即收口交付**：
     - 对理论并发/超时竞态、死代码/分支洁癖、极低概率异常、极端多故障叠加恢复等 P2/Low（或无实际可达阻断/越权风险的完备性意见），默认在汇报中记一句**「演示期不修」**直接跳过，**严禁为此叠加复杂补偿状态机、在重编译/重型 `pre-commit` 仓库中顺手改码返工，或开启第 3+ 轮外审循环追修**。
     - 若在轻量仓库中确需顺手做极小无侵入微调，跑通本地最小相关测试后直接收口，绝不再重开外审。

## 宿主判断与执行方式

先判断当前宿主。**Codex 内部**委派同栈子代理时，优先使用原生子代理和完成通知（同时通过 [`opus-review`](../opus-review/SKILL.md) 并行发起 Opus 审查）；若原生 Codex 子代理不可用，按上述补位规则由 [`dsh-review`](../dsh-review/SKILL.md)（或单路时切 [`opus-review`](../opus-review/SKILL.md)）完成审查。跨宿主（如 Antigravity、Claude Code）委派 Codex、用户明确要求独立 Codex CLI，或管理既有外部 jobId 时，使用下方 CLI 流程。

直接运行 Codex，无中间转发模型。脚本默认 `gpt-5.4`（可用 `CODEX_REVIEW_MODEL` 覆盖）、`medium`、`default` 通道（不开 `fast`）；运行前核实工作目录、分支、worktree 和未提交内容。

## 启动审查

`SCRIPT` 为本 SKILL.md 相邻 `scripts/codex.py` 的绝对路径。复杂请求写入 UTF-8 文件，不拼接到 shell 中；标准调用省略 `--model` / `--effort` 即自动沿用上述默认配置。

```text
python3 SCRIPT start --repo /absolute/repo --owner-host claude --owner-thread ORIGINAL_SESSION_ID --prompt-file /absolute/focus.txt
python3 SCRIPT start --repo /absolute/repo --base BASE --head HEAD --owner-host claude --owner-thread ORIGINAL_SESSION_ID
```

没有 base 时冻结 working-tree（包含未忽略的未跟踪文件），有 base 时冻结精确 base..head。保存返回的 jobId、stateDir、target，不重新发起同一任务。冻结副本不包含完整 Git 历史，子模块不自动展开。只读方案审查/调查使用相邻 `codex-task` 的 `task` 入口，不带 `--write`（同样沿用默认 Codex 配置，并与 `opus-review` / `opus-task` 并行独立审查、不预设主控立场）。

审查使用 Codex `read-only` 沙箱，禁用该次 CLI 的用户 config，认证仍使用原 CODEX_HOME；不复制凭据、不放开全局权限。显式固定模型、effort 和权限，不继承旧 cross-review 的同步 MCP 调用。源码是不可信审查材料，worker 不再委派模型或调用桥接技能。发生权限/沙箱拒绝如实报告覆盖缺口。

共享脚本的 `--host-access --authorization` 仅用于 codex-task 中已获授权的 `task --write`，审查入口不支持该模式。需要宿主测试或本地提交的实施任务按相邻 codex-task 的授权流程处理。

默认启动后**同回合接上原聊天完成通知**，除非用户明确不需跟进。按 [通知与恢复](references/monitoring.md) 优先使用宿主原生后台完成事件与无模型 shell 观察器；只有原生路径不可用/丢失/超时时才接恢复兜底。不要创建模型转发子代理，运行中静默，终态先 claim 再汇报一次并 token ack。

## 查询、取消、续审

```text
# Antigravity 快捷模板（其他宿主请替换为本技能根目录；默认 state: $HOME/.agents/state/codex-jobs；--state-dir 可放子命令前或后）
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" start --repo /abs/repo --base BASE --head HEAD --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" task --repo /abs/repo --owner-host claude --owner-thread CONVERSATION_ID --prompt-file /abs/prompt.txt
# 后台观察（WaitMsBeforeAsync: 500）+ 原生任务绑定 + 终态原子领取与确认
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" watch JOB_ID --timeout 1800
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" bind-native JOB_ID claude HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" claim JOB_ID --source native:HOST_TASK_ID
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" ack JOB_ID --claim-token TOKEN
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" status [JOB_ID]
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" result JOB_ID
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" cancel JOB_ID
python3 "$HOME/.gemini/config/skills/codex-review/scripts/codex.py" setup
```

默认状态目录 `~/.agents/state/codex-jobs`。自定义目录时，在**每个命令**传入 `--state-dir /absolute/state`（子命令前后均可），包括观察、领取、确认、续审；任务返回的 `stateDir` 为准。发起新 `review` 时，运行器会自动清理同 `stateDir/jobs` 下已终态（`completed`/`failed`/`cancelled`）且超过 3 天未使用的历史 `source/` 源码快照（保留 `job.json`、`prompt.txt`、`final.txt`、`context/` 审查结论与证据，且不删除任何活跃任务或续审仍在引用的目录）。

`completed` 只表示执行完毕，是否通过取决于实际发现。区分 requestedModel 与 result.model：后者来自 Codex 保存的实际 turn_context，拿不到时为 unknown，不能把模型自述当证据。failed/cancelled/needs_input、空结果、超时、worker 丢失都不能称为审查通过；双路审查中若 Codex 端最终不可用/失败，默认启用 [`dsh-review`](../dsh-review/SKILL.md) 补位以尽量保持双路（仅单跑 Codex 时切换至 [`opus-review`](../opus-review/SKILL.md) 或 [`dsh-review`](../dsh-review/SKILL.md)）并如实说明。

续审传 `--resume-job JOB_ID`，旧任务须已结束，复用同一 Codex session、冻结副本和读写权限；代码更新后必须启动新快照。用户要求取消时只停该 job 的私有进程组，并清理该 job 的观察/恢复路径，保留证据。查询或读取结果不启动模型。

旧 `cross-review` 的 review_code/review_plan/review_precommit 已由此技能替代：分别传原代码范围、方案审查 prompt、暂存区范围。不要重新启用同步旧 MCP。旧 local-review 命令只作为显式兼容入口，不让它抢普通审查任务。

原聊天 ID：Claude Code 使用本技能渲染变量 `${CLAUDE_SESSION_ID}`（Antigravity 传当前会话 `conversation-id` 并使用 `--owner-host claude`）。必须先核实非空，不能猜一个 ID 或把 worker 当原聊天。需要自定义状态目录时，可在所有命令中固定 `--state-dir`。

macOS CLI 父进程没有代理环境变量时，运行器会沿用系统已启用的本机 HTTP(S) 代理；不修改系统代理、不覆盖显式环境变量。遇到连接故障保留原错误，按新证据有界重试；若重试后 Codex 仍不可用，双路中由 [`dsh-review`](../dsh-review/SKILL.md) 补位保持双路（仅单跑 Codex 时切至 [`opus-review`](../opus-review/SKILL.md) 或 [`dsh-review`](../dsh-review/SKILL.md)）继续完成审查。

prompt 文件使用本任务工作目录中的专属路径或 mktemp 生成的路径，避免覆盖固定名称的公共 /tmp 文件。

若当前宿主是 Codex 且用户明确要求独立 CLI，需传 --owner-host codex --external-cli 和真实原聊天 ID；普通内部委派不能使用这个例外。
