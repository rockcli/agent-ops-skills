---
name: tui-continue-scheduler
description: Schedule a one-shot or delayed "continue" (or any text) into an already-running interactive agent TUI — Claude Code in Ghostty, and similar — with delivery proof read back from the session log. Use when the user asks to 定时给某个对话发 continue、N 小时后让它继续、"过一会儿再看它一眼"、schedule a nudge/continue/resume into a running Claude Code session, wake an idle TUI session later, or automate a delayed keystroke into a Ghostty pane. Also covers diagnosing why a scheduled injection silently did nothing.
---

# 定时给运行中的 TUI 会话发消息

给一个**已经在跑**的交互式 agent TUI（典型：Ghostty 里的 Claude Code）在指定时刻注入一条文本并回车，
并且**回读 session 日志确认真的落库**。不是开新会话，是往既有会话里续一句话。

核心难点不是"定时"（launchd 一行就够），而是**注入本身会静默失败**。这个 skill 的价值主要在踩坑清单。

## 什么时候用 / 不用

| 用 | 理由 |
|---|---|
| 让某个挂着的 Claude Code 对话在几小时后自己继续 | 典型场景就是本 skill 的由来 |
| 需要"送达证明"的定时 nudge | 不确认落库就等于没发 |
| 排查"定时发了但没反应" | 见下面的坑清单，命中率很高 |

| 不用 | 改用 |
|---|---|
| 派新任务给 agent | 直接起一个新 tab / headless（见 `ghostty-multi-agent-orchestration`） |
| 操作 dsh 对话 | `dsh-session-bridge`（有正规 API，不需要注入 keystroke） |
| 操作 Codex 任务 | `codex-send-continue` |
| 只是要看状态、不发送 | 只读，别注入 |

## 组件

```
本目录/
├── SKILL.md
├── scripts/
│   ├── tui-continue.sh      # 发送器：定位 → 注入 → 回读验证（可手工独立调用）
│   └── task/
│       ├── install.sh       # 装/卸一次性 launchd 任务
│       └── run-once.sh      # launchd 执行体：跑一次 + 记日志 + 自毁
└── references/
    └── pitfalls.md          # 踩坑详解与实证数据
```

**安装时建议复制到自己拥有的目录**（如 `~/.agents/scripts/<任务名>/`），不要直接引用 skill 目录：
skill 目录可能被同步/更新覆盖，而 launchd 到点执行时只认 plist 里那条写死的路径。

## 快速用法

```sh
S=<本skill>/scripts

# 1. 先 dry-run：只看它会选哪个目标，不发送（排查首选）
sh $S/tui-continue.sh --cwd /path/to/project --dry-run

# 2. 确认无误后手工真发一次，验证整条链路
sh $S/tui-continue.sh --cwd /path/to/project

# 3. 装定时任务（默认 +5 小时；参数透传 date -v）
sh $S/task/install.sh --cwd /path/to/project
sh $S/task/install.sh --cwd /path/to/project --offset +2H

# 4. 卸载 / 看状态
sh $S/task/install.sh --uninstall
launchctl print "gui/$(id -u)/com.user.tui-continue-once" | grep -A7 calendar
cat /tmp/tui-continue-task.log        # 到点执行后看这里
```

退出码：`0`=成功落库 · `1`=参数/环境错 · `2`=定位不到或有歧义（**未发送，安全失败**）·
`3`=注入失败 · `4`=已注入但未确认落库（需人工看）

## 工作流（照做）

1. **先定位，不发**。`--dry-run` 确认选中的进程 / 会话 / 面板都对。
2. **手工真发一次**。这是唯一能证明链路通的方式，顺便让 session 日志留下证据。
3. **确认 Claude 真响应了**，再装定时任务。
4. **验 launchd 语境**（首次在新机器/新 terminal app 上做时**必做**）：
   自动化权限（TCC）按父子进程链授权，你从终端直接跑通 ≠ launchd 拉起时也能跑通。
   用一个 20 秒触发的一次性探针 agent 实测，测完拆掉。见 `references/pitfalls.md`。
5. **验「到点真的会启动」**（同样必做）：`launchctl print` 显示注册 ≠ 会触发。
   用 `StartInterval=20` 的短周期探针 + 假发送器（**不要**用真发送器，会真发消息），
   确认 `runs` 递增、日志写出、自毁生效。
6. 装完复核 `launchctl print` 里的 `run interval`，并确认 sentinel 不存在。

## 必须遵守的硬约束

- **`input text` 与 `send key "enter"` 分两次 osascript 调用，间隔 ≥2s**（用 3s）。
  同一次调用里连着写，消息可能根本不进 session。
- **会话身份取 session 文件里第一条 `cwd`，不是最后一条**。子 agent（sidechain）会写入
  自己的 cwd，取最后一条会把主会话误判为不匹配、进而选中一个已闲置的旧会话。
- **`AppleScript 的 tab` 是字面量字符串 `"tab"`，不是制表符**。枚举结果别按 `\t` 切分。
- **写纯 POSIX sh**。本机非交互 shell 是 zsh，`mapfile`、`case` 里带空格的模式、
  `"?"` 这类模式都会出问题（`NO_BARE_GLOB_QUAL` 会让它变语法错误）。
- **不硬编码 terminal UUID**。运行时动态枚举，否则面板重建后就失效。
- **目标不唯一时拒绝执行**，绝不猜。多个 claude 进程、多个候选会话 → 报错退出。
- **必须回读验证**。只认「jsonl 尾部新增匹配的 user 记录」，不靠截图、不靠 osascript 返回值。
- **定时不要用 `StartCalendarInterval`**。LaunchAgent 带 `Year` 键指向未来时刻会
  **静默永不触发**（`runs=0`、无日志、无报错，注册状态却显示正常）。
  用 `StartInterval` 轮询 + runner 自门控。
- **plist 的 `ProgramArguments` 显式走 `/bin/sh <脚本>`**。直接执行脚本会让
  `xpcproxy exit(78) EX_CONFIG`，任务根本不起来。
- **装完必须验证"到点真的会启动"**。`launchctl print` 显示已注册 ≠ 会触发。
  用短 interval 探针实测 `runs` 是否递增。

## 排错

| 现象 | 原因 |
|---|---|
| 定时任务"成功"但对话没反应 | 注入静默失败或选错会话；看 `/tmp/tui-continue-task.log` 的结论行 |
| `runs=0`、无日志、无报错 | `StartCalendarInterval` 静默漏触发 → 改 `StartInterval`（坑 9） |
| `last exit code = 78` | `ProgramArguments` 直接指向脚本 → 改成经 `/bin/sh`（坑 10） |
| 退出码 2 且说没有目标 | Claude 进程或 Ghostty 面板不在了；**这是安全的**，没误发 |
| 选中的是旧会话 | 命中了"最后一条 cwd"的坑，见 `references/pitfalls.md` 第 2 条 |
| 终端里能发、launchd 发不了 | TCC 自动化权限的父子链差异，跑第 4 步探针确认 |
| 测试里改了环境变量却不生效 | launchd 不继承 shell 的 `export`（坑 11） |
| 发送后没落库 | 面板 cwd 与目标不一致（有人在面板里 `cd` 过）；Ghostty 无 `tty` 属性可匹配 |

细节、实证数据和诊断命令见 `references/pitfalls.md`（11 类坑）。
