# 踩坑详解与实证数据

全部是 2026-09-21 实战踩出来的，附当时的实际观测数据，方便判断是否命中同一个坑。

---

## 1. `input text` 与 `send key "enter"` 必须在两次 osascript 调用里

**现象**：脚本退出码 0、两个调用都报成功，但 session 日志里**没有** user 记录，Claude 毫无反应。

**原因**：同一次 osascript 调用里 `input text` 后紧跟 `send key "enter"`，消息可能根本没进 session。

**正确做法**：分两次调用，中间 `sleep` ≥2s（本 skill 用 3s）。

```sh
osascript -e "tell application \"Ghostty\" to input text \"continue\" to (terminal id \"$TID\")"
sleep 3
osascript -e "tell application \"Ghostty\" to send key \"enter\" to (terminal id \"$TID\")"
```

**唯一可信的送达证据**：session 日志尾部新增一条内容匹配的 `user` 记录。

---

## 2. 会话身份必须取【第一条】`cwd`，不是最后一条 ⚠️ 最隐蔽的坑

**现象**：脚本"成功"选中了一个 session，但那是**几小时前就已闲置的旧会话**。定时任务会跑到一个
没人看的对话里去，而你以为发对了。

**根因**：同一个 session 文件里 `cwd` 字段有**多个值**。子 agent（sidechain）会把自己的 cwd 写进
父会话文件。实测数据：

```
b2689d05-....jsonl:
  '/Users/alice/projects/my-app'           x 282   ← 主会话
  '/Users/alice/projects/my-app/web'       x 121   ← sidechain 写的
```

按"最后一条 cwd"解析会得到 `.../pro`，与目标 cwd 不匹配 → 该会话被整条丢弃 →
解析器退而选中 mtime 最新的**其它**会话（当时是 `5ef683ba`，20:07，已闲置 2.8 小时）。

**正确做法**：读文件时**第一条**带 `cwd` 的记录才是会话身份，命中即 `break`。

```python
for line in fh:
    d = json.loads(line)
    c = d.get("cwd")
    if c:
        first_cwd = c
        break          # ← 关键：不要继续往后覆盖
```

**验证方式**：dry-run 输出的 session 必须与"当前真正在被写入的那个文件"一致。
可交叉核对：`ls -lt ~/.claude/projects/<转义目录>/*.jsonl | head -1`。

---

## 3. AppleScript 的 `tab` 是字面量字符串 `"tab"`

**现象**：枚举 Ghostty 面板后按 `\t` 切分，`$2` 永远不匹配 cwd。

**根因**：AppleScript 里 `` `tab` `` 被强制转成 unicode text，结果是**四个字符 `tab`**，不是制表符。
`od -c` 实证：

```
... 0 1 7 0   t   a   b   /   U   s   e   r   s ...   ← 字面 t a b
```

**正确做法**：用不会出现在路径里的分隔符，例如 `|||`，再用 `awk -F'\\|\\|\\|'` 切分。

---

## 4. 执行环境是 zsh，不是 bash

**现象**：脚本用 `#!/bin/bash` 且写了 bash 专属语法，但实际由 zsh 解析，报出各种莫名错误：

| 写法 | 在 zsh 下的后果 |
|---|---|
| `mapfile -t ARR < <(...)` | `mapfile: command not found` |
| `case "$x" in *" -p "*) ;; esac` | `syntax error near unexpected token ';;'`（模式里带空格） |
| `case "$tty" in ""\|"?"\|"??") ;; esac` | `syntax error`（`NO_BARE_GLOB_QUAL` 让 `?` 变 glob qualifier） |

注意第 3 条：**函数体在调用时才解析**，所以同样的代码放在函数里，只有执行到才会暴露。

**正确做法**：写纯 POSIX sh，用 `grep` 替代 case 模式匹配，用换行分隔的字符串替代数组。
提交前用三种 shell 都过一遍语法：

```sh
sh -n f.sh && zsh -n f.sh && bash -n f.sh
```

---

## 5. TCC 自动化权限：终端里能跑 ≠ launchd 拉起时能跑

**现象**：手工在终端里注入成功，但由 launchd 定时触发时静默失败。

**根因**：macOS 自动化权限按**父子进程链**授权。你从终端跑，责任进程是你的 terminal；
launchd 拉起时责任进程是 `launchd`，是另一条信任链。

**正确做法**：**必须实测**，不要假设。用一个 20 秒触发的一次性探针 agent：

```sh
# 探针脚本：往一个临时面板注入 touch <marker>，然后看 marker 是否出现
cat > /tmp/probe.sh <<'EOF'
#!/bin/bash
exec >>/tmp/probe.log 2>&1
echo "start uid=$(id -u)"
/usr/bin/osascript -e "tell application \"Ghostty\" to input text \"touch /tmp/probe_marker\" to (terminal id \"<TID>\")"
echo "input rc=$?"
sleep 3
/usr/bin/osascript -e "tell application \"Ghostty\" to send key \"enter\" to (terminal id \"<TID>\")"
echo "enter rc=$?"
EOF
chmod +x /tmp/probe.sh
# plist: StartInterval=20, RunAtLoad=false → bootstrap → 等 30s → 看 /tmp/probe.log
```

实测通过（`PROBE PASS`）才说明定时任务能真正控制 GUI。

---

## 6. 测试用的目标面板必须是**交互式**进程

**现象**：用 `#! /bin/zsh` + `echo` + `sleep 240` 的脚本当测试面板，注入的字**永远不生效**。

**根因**：那个 zsh 正在 `sleep`，**根本不读 stdin**。注入的字节进了 TTY 输入缓冲，但无人消费。
而真实目标（Claude Code TUI）是一直在读 stdin 的，所以这个测试**根本没模拟到真实场景**。

**正确做法**：测试面板必须是交互式 shell：`exec /bin/zsh -f -i`。
如果测试失败，先怀疑测试装置，再怀疑通道。

---

## 7. Ghostty terminal 没有 `tty` 属性

**现象**：想按进程的控制终端（tty）匹配面板，比 cwd 更稳，但：

```
Can't make tty of item 1 of every «class Gtrm» ... into type Unicode text. (-1700)
```

**后果**：只能用 `working directory` 匹配面板。若有人在 Claude 面板里 `cd` 过，可能匹配不上。

**缓解**：匹配失败就是退出码 2、**不发送**——失败是安全的，不会误发到别的对话。
若需要更稳的匹配，得换用别的机制（例如从会话日志里的 tty 信息反查）。

---

## 8. 其它小坑

| 坑 | 说明 |
|---|---|
| plist 里 `Month`/`Hour` 用 `09` | 会被当八进制解析。用 `date +%-m` 去前导零 |
| 一次性任务不自毁 | 仍建议脚本自毁（bootout + rm plist + rm config） |
| 自毁后重复触发 | 加 sentinel 文件（`$LOG.fired`）双保险 |
| 测试误伤生产 agent | 测 runner 时若用同一个 `LABEL`，`launchctl bootout` 会把真 agent 卸掉。实测踩到过。**要测就改 `LABEL`/`PLIST`/`CONFIG` 指向临时值**，且 sed 替换后要 `grep` 复核变量都换干净了 |
| Mac 睡眠 | 用 `StartInterval` 方案后睡眠/唤醒天然没问题 |
| 关闭 Ghostty | 枚举不到面板 → 退出码 2、不发送（安全） |

---

## 9. ⚠️ `StartCalendarInterval` 会静默永不触发（最坑的一条）

**现象**：任务装好了、`launchctl print` 显示注册正常、到点后**什么都没发生**。没有日志、没有报错、
脚本从未被执行：

```
state = not running
runs = 0
last exit code = (never exited)
```

`log show --predicate 'process == "launchd"'` 里**连一条记录都没有**——launchd 从未尝试启动它。

**先排除睡眠问题**：查电源断言与会话日志，确认机器当时醒着（有进程持续持有
`NoIdleSleepAssertion`，日志跨夜连续）。本案例确认不是睡眠，是 launchd 自己漏掉了。

**根因**：macOS 的 LaunchAgent 用 `StartCalendarInterval` 带 `Year` 键指向未来某一时刻时，
会静默地永不触发。已知的 launchd 行为坑，且**失败无声**——最难排查的一类。

**正确做法**：改用 `StartInterval` 轮询 + 脚本自门控：

```xml
<key>StartInterval</key><integer>300</integer>   <!-- 每 5 分钟醒一次 -->
<key>RunAtLoad</key><false/>
```

runner 每次被启动时读配置里的 `DUE_EPOCH`，未到点就**静默 exit 0**（不写日志，避免刷屏），
到点才发送、然后自毁：

```sh
NOW=$(date +%s)
if [ "$NOW" -lt "$DUE_EPOCH" ]; then exit 0; fi
```

附带好处：天然免疫睡眠/唤醒（唤醒后下一个 interval 自然补上），且到点时间可晚于安装时刻任意长。

**教训**：装完后**必须验证"到点真的会启动"**，不能只看注册状态。
`launchctl print` 显示已注册 ≠ 会触发。用短 interval 探针实测 `runs` 是否递增。

---

## 10. `ProgramArguments` 直接指向脚本会 `exit(78) EX_CONFIG`

**现象**：plist 里把脚本路径直接当可执行文件：

```xml
<key>ProgramArguments</key>
<array><string>/path/to/run-once.sh</string></array>
```

launchd 启动它时 `xpcproxy` 立刻 `exit(78)`（`EX_CONFIG`），
脚本的日志文件都没被创建（脚本体一行都没跑）：

```
state = spawn scheduled
runs = 1
last exit code = 78: EX_CONFIG
```

**正确做法**：显式经过 shell：

```xml
<key>ProgramArguments</key>
<array>
	<string>/bin/sh</string>
	<string>/path/to/run-once.sh</string>
</array>
```

实测这样正常（`runs=1`、`exit code = 0`、日志正常、自毁正常）。

**排查提示**：`log show --predicate 'process == "launchd"'` 里看到
`xpcproxy exited due to exit(78)` 就是这一类。注意系统自身的 `com.apple.contactsd`
也有同样的 78，别被误导。

**排除过的错误假设**（省得你再试一遍）：

- ❌ 不是文件权限。曾怀疑 `701`（属主无读位）导致 shell 读不到——实测 `/bin/sh` 能正常读并执行
  701 的文件（属主默认有读权限）。
- ❌ 不是 `EnvironmentVariables`。带与不带都正常。
- ❌ 不是 `RunAtLoad=false`。

**定位方法**：先用**最小对照任务**（两行脚本，只 `echo` 到文件）确认 launchd 本身能跑，
再逐步加回差异项、一次只加一个。直接排查复杂脚本会浪费时间。

---

## 11. launchd 不继承你 shell 里的 `export`

**现象**：测试时 `export TUI_TASK_DIR=/tmp/x` 再 `launchctl bootstrap`，任务跑起来仍用默认路径。

**原因**：job 的环境来自 plist 的 `EnvironmentVariables`，**不继承**当前 shell。

**做法**：测试 plist 显式写 `EnvironmentVariables`；生产版避免依赖环境变量——
把目标 cwd 与到点时间写进配置文件（如 `~/.tui-continue-task/config`），runner 每次自己读。

---

## 诊断命令速查

```sh
# 目标到底是谁
sh tui-continue.sh --cwd <项目> --dry-run

# 当前真正在被写入的 session（与 dry-run 结果交叉核对）
ls -lt ~/.claude/projects/<转义目录>/*.jsonl | head -3

# 某会话里出现过哪些 cwd（判断是否踩坑 2）
python3 -c "
import json,sys,collections
c=collections.Counter()
for l in open(sys.argv[1],errors='ignore'):
    try: d=json.loads(l)
    except: continue
    if d.get('cwd'): c[d['cwd']]+=1
[print('  %r x%d'%(k,v)) for k,v in c.items()]
" <session.jsonl>

# 任务是否还挂着 / 何时触发
launchctl print "gui/$(id -u)/com.user.tui-continue-once" | sed -n '/descriptor = {/,/}/p'

# 到点执行结果
cat /tmp/tui-continue-task.log

# Ghostty 面板清单（含 cwd）
osascript -e 'tell application "Ghostty"
set out to ""
repeat with w from 1 to (count of windows)
repeat with t from 1 to (count of tabs of window w)
repeat with tm in terminals of tab t of window w
set out to out & (id of tm) & " ||| " & (working directory of tm) & linefeed
end repeat
end repeat
end repeat
return out
end tell'
```
