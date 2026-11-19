# macOS 数据布局与分级参考

分析 macOS 扫描结果时读这份。讲"东西存在哪、怎么辨认、归哪一级"。

## 关键目录

| 目录 | 装什么 | 典型分级 |
|---|---|---|
| `~/Library/Caches/*` | 应用/工具缓存（浏览器、Homebrew、pip、playwright） | 🟢 可自动清 |
| `~/.npm/_cacache`、`~/.gradle/caches`、`~/.cargo/registry/cache` | 已核实的开发缓存子目录 | 🟢 |
| `~/.cache`、`~/.cargo`、`~/.m2`、`~/.docker` | 混合状态、配置、凭据、模型或本地 artifact | 🟡，先逐项辨认 |
| `~/Library/Developer/Xcode/DerivedData` | Xcode 构建缓存 | 🟢 |
| `~/Library/Developer/CoreSimulator`、`iOS DeviceSupport` | 模拟器/设备支持及可能的测试数据 | 🟡，用 Xcode 工具管理 |
| `~/Library/Containers/<UUID 或 bundleid>` | 沙盒应用数据（聊天记录、离线视频、设置） | 🟡 多为用户数据 |
| `~/Library/Application Support/*` | 应用数据（Chrome Profile、Claude VM、飞书） | 🟡 |
| `~/Downloads` 里的 .dmg/.pkg | 安装包残留 | 🟢 |
| `/Applications/*.app` | 应用本体 | 🔴 仅当重复/想卸时上灯，否则归蓝色 |
| 系统文件、APFS 本地快照 | 系统 | 不上灯，归蓝色"系统及其他" |

当系统设置里的 “System Data” 或 “Other Users & Shared” 异常大时，不要根据 UI 分类直接给删除建议；读 [macos-system-data.md](macos-system-data.md) 并运行专项扫描。

## 辨认"神秘 UUID 容器"

`~/Library/Containers/` 下 UUID 命名的大目录，要查清属于哪个 App：
- `ls` 进 `Data/Documents/`、`Data/Library/`，找带 bundle id 的子目录（如 `com.bilibili.bbad` → 哔哩哔哩）
- 大头常藏在隐藏目录（如 `.Downloads/` 里的 `.bilitask` 离线视频）
- 仍只读，别动文件

## 间接释放（写进 long_term，不上红灯）

- 系统"可清除空间"磁盘紧张时自动回收
- 重启释放部分 swap / 临时快照
- `brew cleanup --prune=all`、清 Xcode DerivedData
- 调整 Time Machine 本地快照保留策略

## 删除机制

`server.py` 在 macOS 用 osascript 调访达入废纸篓；首次弹自动化授权，点允许。

## 应用卸载残留

当用户明确说应用已卸载并要求“相关的全部清掉”时，先确认 `.app` 不存在且无相关进程，再按 bundle id 精确检查：

- `~/Library/Application Support/<App>`
- `~/Library/Caches/<App 或 bundle id>`、`ShipIt`
- `~/Library/Preferences/<bundle id>.plist` 与 `Preferences/ByHost`
- `~/Library/HTTPStorages`、`WebKit`、`Saved Application State`、`Logs`
- 应用自己的隐藏目录、扩展目录和 CLI 入口

只删除已确认属于该应用的精确路径。不要用应用名做全 home 模糊删除，避免误删同名项目、源码或文档。
