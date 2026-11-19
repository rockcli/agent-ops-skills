# 原聊天完成通知：原生优先，定时兜底

`opus-review` 和 `opus-task` 共用此策略（与 `codex-review`、`dsh-review` 采用统一协议）。任务进程、观察进程和原聊天是三个不同生命周期。
等待不启动额外审查或转发模型；观察进程的超时/退出不能当成审查成功。

本文件只处理**已经选择外部进程/跨宿主调用**的任务。宿主自身能原生委派的模型任务直接使用原生子代理和内置完成通知，
例如 Codex→Codex 原生子代理、Claude Code→Opus 原生子代理；不经过本文件的 shell 观察器或定时兜底。
明确管理已有外部 jobId 时保留其原记录；若指定的审查模型（Opus 或 Codex）不可用，按审查技能规则自动切换到另一个模型继续完成审查。

## 选择宿主能力

- **Claude Code / Antigravity**：有后台命令（Bash `run_in_background` 或异步命令/schedule）和 completion 通知时，优先原生后台观察。
- **Codex/其他宿主**：以当前工具契约为准。仅有 exec/sessionId、write_stdin、wait 或可轮询状态，不证明完成后会自动唤醒原聊天；这种环境直接使用定时兜底。若宿主明确提供可靠原生完成通知，可用原生方式。
- 跨回合继续聊天本身不需要定时器；宿主退出、观察任务丢失/超时、或需要持久恢复时才切换兜底。
  后台任务通知可能随所属会话退出而失效；持久任务记录仍可重新接管。

不要默认同时启动后台观察和定时器。先检查 job 的 nativeTask 与 monitorId，复用仍有效的路径。
已有旧版定时监控时，先核实并迁移其 prompt 到下面的 claim 协议，再切换；不直接叠加第二条未去重路径。

## 无模型后台观察

启动业务任务取得 `jobId` 后，用当前宿主的后台工具启动脚本内置的无模型观察器（`OPUS_SCRIPT` 为本文件相邻 `../scripts/opus.py` 的绝对路径；旧 `completion.py` 仅作兼容保留）：

```text
python3 OPUS_SCRIPT watch JOB_ID --state-dir ABSOLUTE_OPUS_STATE
```

`opus.py` 同时覆盖 review/task，state 默认 `~/.codex/state/opus-review`；自定义时支持 `--state-dir`（子命令前后均可）或环境变量 `OPUS_REVIEW_STATE_DIR`。所有后续命令保持同一绝对状态路径。

观察器只轮询同一任务，运行中 stdout/stderr 均静默，不打印进度心跳；默认每 5 秒检查，最多观察 3600 秒，可用 `--interval` / `--timeout` 显式调整。
终态输出一份 JSON 后退出，由宿主的后台完成通知回到**发起聊天**。宿主返回真实后台 ID 后保存绑定：

```text
python3 OPUS_SCRIPT bind-native JOB_ID claude|dsh|codex HOST_TASK_ID --state-dir ABSOLUTE_OPUS_STATE
```

绑定前核对宿主 ID 与原聊天归属。替换已绑定任务前，确认旧观察已结束/不可恢复，再传 `--replace OLD_ID`。
不要另建一个模型子代理专门等待，不把普通 `wait` 的超时结果当终态。

## 所有终态回调共用原子领取

原生 completion、定时兜底、重投通知或人工补查，**报告前都重新领取同一任务**，不直接引用旧工具输出发通知：

```text
python3 OPUS_SCRIPT claim JOB_ID --source native:HOST_TASK_ID
```

兜底来源改为 `fallback:ACTUAL_SCHEDULE_ID`。CLI 返回的 claimToken 是本次通知所有权，默认租期300秒。
MCP 对应 `review_claim(job_id, source)`；Opus MCP ack 用 `review_monitor(job_id, ack=true, claim_token=TOKEN)`。
若当前 MCP 尚未加载 review_claim/claim_token，直接使用这些 CLI，不以旧版 monitor 的 notify 字段代替原子领取。

- `notify=true`：仅领取者读取实际 result/error/target，准备原聊天通知，然后携 token ack 并停止本任务观察/兜底。
- `notify=false,stopMonitor=true`：已经处理，静默清理本路径，不能重复通知。
- `notify=false,stopMonitor=false,pendingNotification=true`：另一条路径正在处理，不重复通知。已有有效兜底时保留它；
  原生路径若无兜底且无法确认领取者仍会完成，在原聊天重新注册同一 job 的无模型后台观察，等待 ack 或租约到期。
  观察脚本遇到活动租约会静默等到 ack/到期再返回；若原生观察不可用，接租约到期恢复检查的定时兜底。
  必须保留一个已核验的恢复触发源，不能只静默退出并假定租约到期会自动唤醒。
- 未终态：静默，不提前 ack；核实仍有有效观察/兜底。若原生观察已结束，按上述恢复规则重新注册无模型观察。

```text
python3 OPUS_SCRIPT ack JOB_ID --claim-token TOKEN
```

CLI 有活动领取时拒绝无 token/错误 token 的 ack，旧任务无领取时仍兼容原 ack。
ack 后所有路径返回 notify=false。取消任务或停止观察时，保留状态和结果；只停止本任务的观察与兜底，不终止无关任务。
报告区分 completed（执行完成）和审查通过；failed/cancelled/needs_input、冻结范围、真实模型和未验项如实说明。
通知、ack 和宿主状态不能跨系统原子提交，租约解决并发重复，不能保证崩溃于发送与 ack 之间的严格 exactly-once。

## 何时接定时兜底

宿主没有可靠原生完成唤醒，或后台工具报告观察器丢失、`watchStatus=observer_timeout/observer_failed` 时，
核对业务任务仍需跟进，在同一原聊天给**同一 jobId**接定时兜底，不重新启动审查。
观察超时输出 fallbackRequired=true，退出码4；查询故障为2。这些是观察器故障，不是业务任务的 failed。
宿主原生后台工具本身失败也按此处理，不能只依赖脚本 stdout。

- Codex：原生 automation_update heartbeat，destination=thread，通常每分钟；不创建新聊天或 standalone cron。
- Antigravity：原生 `schedule` 工具。
- 其他宿主：只有确有可恢复的原生调度接口才使用；否则明确报告未接上持久跟进，保留 jobId。

保留实际调度 ID、原聊天归属、完整命令路径。Opus 用 `bind-monitor JOB_ID SCHEDULE_ID`；替换须先核实旧监控停用，再 `--replace OLD_ID`。
绑定失败停止刚创建的调度，不留下孤儿监控。已终态可以直接 claim/处理，无需新建定时器。

兜底 prompt 必须含实际 jobId、状态路径、来源 ID 和如下行为：

```text
仅检查指定原聊天任务，不启动审查、修改代码、push、部署或操作其他任务。
运行 ENGINE_SCRIPT monitor JOB_ID（携固定 state）。
运行中静默结束，最终内容零字符，不输出“暂无变化”等结束语。
终态必须先 ENGINE_SCRIPT claim JOB_ID --source fallback:本调度ID；只有notify=true才报告实际结果，
随后ENGINE_SCRIPT ack JOB_ID --claim-token 返回TOKEN。
已ack时静默停止本调度；另一路持有租约时静默保留兜底，等后续检查。
处理成功后，Codex 用 automation_update 暂停本自动化，保留其他任务。
检查故障报告监控故障并停止本调度，不推断业务成功；不要对损坏记录循环调用或新建审查。
```

原生完成回调也使用相同 claim/ack 协议；处理成功后若曾启用兜底，一并暂停/删除该 job 绑定的兜底。
已经投递的唤醒不能撤回，收到时仍须 claim 去重。定时器依赖宿主运行和电脑唤醒，不承诺秒级恢复。
