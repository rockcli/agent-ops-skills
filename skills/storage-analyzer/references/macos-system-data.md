# macOS System Data 专项判断

仅在 macOS 的 “System Data / 系统数据”“Other Users & Shared / 其他用户与共享”异常大，或普通 home 扫描解释不了 Data 卷占用时读取。先运行 `scripts/macos_system_data_scan.py`；所有结论仍以当次只读证据为准。

## 先看真实容量

- 使用 `/System/Volumes/Data` 的 `df` 和 APFS container 数据作为真实容量基线；根卷 `/` 的 `df` 不能代表 Data 卷已用空间。
- 系统设置分类会延迟刷新，也会把用户 Library、Docker、Simulator、临时目录和可清除空间归入 System Data。
- `du` 是逻辑占用。APFS clone、只读挂载的 runtime、Docker shared layer 可能重复计数；删除后必须用同一 Data 卷的可用空间变化验收。

## `/private/var/folders`

- 只分析当前用户由 `getconf DARWIN_USER_*_DIR` 返回的 `T`、`C`、`X`，不要删除整个 `/private/var/folders`。
- `T/pytest-of-*`、废弃 Playwright profile 等可再生临时目录，在无进程占用时可列绿灯。
- `X/*.code_sign_clone` 可能是应用更新/代码签名临时克隆。大量历史副本属于异常候选，但必须先退出应用并用 `lsof +D <精确目录>` 确认无人占用；只处理对应 bundle 的 clone 目录。
- `C` 是每用户缓存根，仍需按 bundle id 分类；不要整体清空。
- 扫描超时后确认没有遗留 `du` 进程。不要依赖 macOS 默认不存在的 GNU `timeout` 命令。

## CoreSimulator

- 先看 `xcode-select -p`、`/Applications/Xcode.app`、runtime 列表和最后使用时间。用户仍做 iOS/watchOS 开发时保持黄灯。
- 优先 `xcrun --find simctl`。若只剩 Command Line Tools，可检查 `/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/bin/simctl`。
- 删除前先运行 `simctl runtime delete <uuid> --dry-run`；经用户授权后使用 `simctl runtime delete <uuid>`。该接口会关闭模拟器、卸载磁盘并清理 secure storage。
- 不手删 `/Library/Developer/CoreSimulator/Volumes`、挂载点或 `/System/Library/AssetsV2`。删除是异步的，等待 runtime 从列表消失并确认 mount、cache 和 Data 卷变化。

## Other Users & Shared

- 用 `dscl . -list /Users UniqueID`、`/Users` 实际目录和登录会话分别确认账号、主目录和当前使用状态。
- `/Users/Shared` 很小不代表 UI 分类错误；空间可能属于另一个用户主目录、Deleted Users disk image 或尚未刷新的统计。
- 用户账号应通过系统设置删除，并明确选择是否删除主目录；不要直接递归删除另一个用户的 home。

## 系统管理项

- APFS 系统更新 snapshot、Preboot、Recovery、VM/sleepimage、`/private/var/db` 默认不作为手工删除候选。
- 可以报告 diagnostics/log 体量，但除非有官方清理入口和明确授权，不直接删除系统数据库或 snapshot。
