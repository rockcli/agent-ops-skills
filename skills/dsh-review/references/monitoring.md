# 原聊天完成通知与恢复

与 [`codex-review`](../../codex-review/references/monitoring.md) 和 [`opus-review`](../../opus-review/references/monitoring.md) 采用完全统一的**原生优先、定时兜底、原子领取去重协议**。DSH 是实际审查者；原聊天宿主可以是 Claude Code、Antigravity、Codex 或 DSH，不按模型名字判断后台通知能力。

## 原生后台完成事件优先

Claude Code 使用 Bash `run_in_background:true`（Antigravity 使用异步后台命令或 `schedule`），由宿主 job completion 自动唤醒发起聊天。脚本内置无模型 shell 观察器，**不是模型转发代理**：

```text
python3 SCRIPT watch JOB_ID --state-dir STATE
```

`SCRIPT` 是本技能 `scripts/dsh.py` 的绝对路径，`STATE` 是 `start`/`task` 返回的绝对 `stateDir`（`--state-dir` 放在子命令前或后均可）。先 `start`/`task` 取得 `jobId`，再后台 `watch`，同回合接好。`watch` 运行中零输出，终态只输出一份紧凑单行 JSON 后退出；默认每 5 秒检查、最多 3600 秒，不启动模型。可用 `--interval` / `--timeout` 调整。

取得真实宿主后台 ID 后绑定，记录归属（与 `codex.py`、`opus.py` 位置参数一致）：

```text
python3 SCRIPT bind-native JOB_ID claude|dsh|codex HOST_TASK_ID --state-dir STATE
```

启动后只向用户简短确认已启动。**不因启动返回值、中间消息或 observer stdout 直接播报结果**；等宿主后台完成事件，再重新 `claim`。普通状态读取不代表会跨回合自动唤醒。没有可靠原生事件、观察任务丢失、`watchStatus=observer_timeout/observer_failed` 时，核实后接兜底，不能把观察器退出当业务任务成功。

不要默认同时注册两条活跃路径。已有 `nativeTask`/`monitorId` 时复用；替换先确认旧路径结束，再 `bind-native`/`bind-monitor --replace OLD_ID`。绑定失败须停止刚建立的本任务观察/兜底，避免孤儿任务；不取消实际业务 job 或其他任务。

## 领取、汇报与确认

原生事件、shell 观察、定时恢复及人工补查都用同一原子领取协议：

```text
python3 SCRIPT claim JOB_ID --source native:HOST_TASK_ID --state-dir STATE
python3 SCRIPT ack JOB_ID --claim-token TOKEN --state-dir STATE
```

- `notify=true`：核对 job、owner、target、真实 `result.response`/`error`，内部准备通知文本；停止本 job 已绑定的恢复触发源，用本次 `claimToken` ack，然后紧接着只在最终答复汇报一次。回调中不先发一遍进度/结果再在最终答复复述。
- `notify=false,stopMonitor=true`：已处理，静默收尾，不重复汇报。
- `notify=false,pendingNotification=true`：其他路径持有租约，静默等待 ack/租约到期，不把相同结果重复播报。必须保留一个恢复源；`watch` 会静默等到 ack 或租约到期。
- 尚未终态：静默，不提前 ack。

默认领取租约 300 秒。过期可由另一条路径恢复；错误 token 不可确认。已关闭的终态任务（非 `needs_input`）支持离线 `claim`，即便宿主重启后也能安全恢复。

## 兜底

优先仍用宿主可自动完成通知的 shell `watch` 恢复同一 `jobId`。宿主后台任务不可用或需跨重启恢复时：Antigravity 用原生 `schedule` 工具；Codex 用原生 `automation_update` heartbeat（`destination=thread`）；DSH 原对话可通过 `schedules` / `stop-schedule` 管理原聊天调度。

`watch` 观察超时返回 `fallbackRequired=true`、退出码 `4`；查询故障退出码 `2`。它们不是模型任务终态，不要重开 DSH 审查。恢复始终用原 `jobId`、`stateDir`、原聊天：

```text
python3 SCRIPT monitor JOB_ID --state-dir STATE
python3 SCRIPT bind-monitor JOB_ID SCHEDULE_ID --state-dir STATE
python3 SCRIPT schedules ORIGINAL_DSH_SESSION_ID
python3 SCRIPT stop-schedule ORIGINAL_DSH_SESSION_ID SCHEDULE_ID
```

兜底提示必须固定命令绝对路径、`jobId`、`stateDir`、`owner` 与来源 ID：运行中静默，终态先 `claim --source fallback:SCHEDULE_ID`，仅领取者汇报并 token `ack`。已 ack 静默停止本调度；活跃租约时保留恢复；查询故障报告监控故障并停止该故障调度，不推断模型成功。

所有回调只处理该任务，不启动模型、不修改业务代码、不操作其他任务。取消 job 后同样按上述协议报告一次 `cancelled`，停止它的观察/调度并保留证据。
