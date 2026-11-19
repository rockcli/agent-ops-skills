---
name: opus-task
description: 跨宿主让 Claude Code/Opus（默认配置见 opus-review）调查或实施任务，管理已有外部任务。宿主能原生委派指定模型时走原生子代理；审查任务使用 opus-review（默认与 codex-review 双路并行独立审查，任一方不可用由 DSH 补位保持双路，复杂或低信心时主控可按需加开 DSH 第三路，不预设主控立场）。
---

# Opus 任务

宿主能原生委派用户指定模型时，直接用原生子代理和内置完成通知，不启动本技能的 shell/CLI/观察器/定时器。
这包括 Claude Code 内部委派 Opus、Codex 内部委派原生子代理。
只有跨宿主调用、用户明确要求独立 Claude CLI，或管理已有外部 jobId 时才使用下面的流程。

**若本技能的只读 `task` 模式被用于方案/设计审查**：必须遵守 [`opus-review`](../opus-review/SKILL.md) 的统一审查规范——默认与 [`codex-review`](../codex-review/SKILL.md) / [`codex-task`](../codex-task/SKILL.md) 一起并行发起独立审查，任一方不可用时由 [`dsh-review`](../dsh-review/SKILL.md)（`dsh.py task`）补位尽量保持双路，复杂或主控信心不足时可加开 [`dsh-review`](../dsh-review/SKILL.md) 作为第三路；Prompt 仅提供客观背景与约束、**严禁预设主控立场**。

Codex 使用 `opus-review` MCP 的 `task_start` 或下方 CLI；Antigravity 直接使用下方 CLI，由 Claude CLI 直接工作，不创建转发子代理。
默认模型与思考强度继承 `opus-review/scripts/opus.py` 默认配置（详见 [`opus-review`](../opus-review/SKILL.md)），用户明确设置优先。任务默认为只读，只有当前任务已授权实施修改才传 `write=true`。
通过 prompt 传递目标、约束、工作目录、已确认事实和验收标准，不依赖整段聊天历史回放。

`repo` 为绝对路径。只读调查在该目录读取；实施任务直接修改该目录，先检查分支、worktree、未提交内容，
需要隔离时先选择合适的工作目录。不把任务授权扩大成 push/merge、发布、部署、安装/删除、凭据或权限变更。
写入模式提供 Read/Glob/Grep/Edit/Write/Bash，并非操作系统沙箱；保持任务原有授权边界，不启用 bypassPermissions。

后台启动后保存 jobId，用 `review_status`、`review_result`、`review_cancel` 查询、读取和按请求取消。
续接已完成任务传入 `resume_job`，保持相同目录和读写权限。不要在原任务运行时重复发起同一工作。
发生权限拒绝会返回 needs_input；按具体缺口处理，不自动扩大权限。

用户要求持续跟进或完成后通知时，读取相邻 `opus-review` 技能的
[完成通知接入](../opus-review/references/monitoring.md)，同回合优先接宿主原生后台任务完成通知；仅在不支持或需要恢复时使用定时兜底。
两条路径共用 jobId 和原子 claim/token ack，运行中静默，终态只通知一次。审查结论、实施修改和验证通过分开报告。

MCP 暂未加载时，解析本 SKILL.md 的相邻目录到绝对路径，调用：

```text
python3 <opus-review-root>/scripts/opus.py task --repo /absolute/repo --prompt-file /absolute/task.txt
python3 <opus-review-root>/scripts/opus.py task --repo /absolute/worktree --prompt-file /absolute/task.txt --write
python3 <opus-review-root>/scripts/opus.py task --repo /absolute/worktree --prompt-file /absolute/followup.txt --write --resume-job JOB_ID
```

保留任务证据，不在聊天结束时自动删除快照、杀死其他任务或清理工作目录。
