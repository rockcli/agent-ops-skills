---
name: codex-task
description: 跨宿主让 Codex（默认配置见 codex-review）调查或实施任务，管理已有外部任务。Codex 内部任务走原生子代理，不触发外部 CLI/观察器；审查任务使用 codex-review（默认与 opus-review 双路并行独立审查，任一方不可用由 DSH 补位保持双路，复杂或低信心时主控可按需加开 DSH 第三路，不预设主控立场）。
---

# Codex 任务

Codex 宿主内部能原生委派指定模型时，直接用原生子代理及其完成通知，退出此技能。跨宿主委派、明确要求独立 Codex CLI 或管理已有外部 jobId 时才继续。

**若本技能的只读 `task` 模式被用于方案/设计审查**：必须遵守 [`codex-review`](../codex-review/SKILL.md) 的统一审查规范——默认与 [`opus-review`](../opus-review/SKILL.md) / [`opus-task`](../opus-task/SKILL.md) 一起并行发起独立审查，任一方不可用时由 [`dsh-review`](../dsh-review/SKILL.md)（`dsh.py task`）补位尽量保持双路，复杂或主控信心不足时可加开 [`dsh-review`](../dsh-review/SKILL.md) 作为第三路；Prompt 仅提供客观背景与约束、**严禁预设主控立场**。

使用相邻 `codex-review/scripts/codex.py`（解析为绝对路径）。默认模型、思考强度与服务通道继承 `codex.py` 内置默认配置（详见 [`codex-review`](../codex-review/SKILL.md)），用户显式指定优先；不经 GPT/Claude 转发模型。

```text
python3 SCRIPT task --repo /absolute/workspace --prompt-file /absolute/task.txt --owner-host claude --owner-thread ORIGINAL_SESSION_ID
python3 SCRIPT task --repo /absolute/worktree --prompt-file /absolute/task.txt --owner-host claude --owner-thread ORIGINAL_SESSION_ID --write
```

默认只读调查（read-only 沙箱）。只有当前任务已授权修改才传 `--write`（workspace-write 沙箱）。普通沙箱写任务需要网络或额外可写目录时，按已有有效授权加 `--network` 和 `--add-dir DIR`（可重复）。先核实分支、worktree、未提交内容和写者归属，保护无关改动。prompt 明确目标、约束、验收，不依赖整段聊天回放。worker 不再委派或调用跨宿主技能。

## 已授权的宿主执行（含浏览器视觉与本地服务自适应）

任务涉及前端 UI/交互、需要监听本地测试端口、使用 Playwright 浏览器截图＋`view_image` 看画面、访问 Docker daemon 或本地 Git 提交，且这些操作已在用户当前有效授权内时，**主控无需等用户专门叮嘱“让它看画面”或等沙箱报错，直接自适应传入 `--write --host-access --authorization "本次已有授权的范围及边界"`**，并在 prompt 中指示 worker 按任务需要自主使用 `playwright-cli` 截图与 `view_image` 核验画面。授权不清楚时只询问缺失范围；授权记录必须基于用户指令，不能凭参数自行创造授权。纯后端且无需本地端口/Docker/浏览器的普通写任务保持默认 `workspace-write`。

```text
python3 SCRIPT task --repo /absolute/worktree --prompt-file /absolute/task.txt --owner-host claude --owner-thread ORIGINAL_SESSION_ID --write --host-access --authorization "用户已授权本任务改代码、启动本机测试服务、使用任务专属 Docker 容器验证并本地提交；不含 push、merge、部署或清理无关资源"
```

该模式显式使用 `danger-full-access` 和 `approval_policy=never`：**解除本次子任务的 Codex 沙箱，技术上具有当前宿主账号可访问的文件、网络和 Docker 权限，并非按端口、命令或目录隔离**；`--network`、`--add-dir` 在此模式下不是限制边界，通常无需附加。任务范围通过 prompt 与授权记录约束，不宣称为操作系统强制隔离。权限仅用于当前任务，不写入全局配置、不复制凭据，不使用 `--dangerously-bypass-approvals-and-sandbox`，仍遵守宿主和组织强制策略。

本地提交只包含本任务变更；测试服务默认监听 loopback；只操作任务相关 Docker 资源，不清理无关容器、镜像或卷。完成前停止自己启动的临时服务和容器，除非用户要求保留。push/merge、部署、发布、安装删除、凭据或权限变更仍须有适用授权；宿主执行本身不授予这些操作。Docker 未安装、daemon 未启动、端口冲突或宿主权限不足仍须报告真实错误，不能笼统归因于 Codex 不支持。

所有模式禁用本次 CLI 用户 config，认证仍沿用原 CODEX_HOME。job/status 记录 `hostAccess`、`authorization`、`sandboxMode`，报告前核对实际模式。续接必须保留原 `--write`、`--network`、`--add-dir`、`--host-access` 和授权原文；不允许续接时改变权限。需要扩权时，先结束/取消旧任务，基于已有用户授权启动新任务并在 prompt 引用旧结果，不能并发双写。

启动保存 jobId、stateDir，并默认同回合按 [通知与恢复](../codex-review/references/monitoring.md) 接原生后台完成通知；无模型 shell 观察器等待，运行中静默，报告前 claim，完成报告后 token ack。不要把读取任务结果的模型代理当观察器。已有任务只查询同一 ID，不重复启动。

status/result/cancel/setup、续接 --resume-job 和固定 --state-dir 的用法见 [审查技能](../codex-review/SKILL.md)。续接不可扩大原有写权限或更换工作目录。分别报告实际修改、验证结果和未验项，不把 completed 等同于任务验收通过。

原聊天 ID：Claude Code 使用本技能渲染变量 `${CLAUDE_SESSION_ID}`（Antigravity 使用当前会话 `conversation-id`）。必须先核实非空，不能猜一个 ID 或把 worker 当原聊天。需要自定义状态目录时，可在所有命令的子命令前固定 --state-dir。

prompt 文件使用本任务工作目录中的专属路径或 mktemp 生成的路径，避免覆盖固定名称的公共 /tmp 文件。
