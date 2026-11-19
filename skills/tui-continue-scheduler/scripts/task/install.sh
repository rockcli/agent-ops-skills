#!/bin/sh
# install.sh —— 安装"到点给某个运行中的 TUI 对话发一次文本"的一次性定时任务
#
# 用法:
#   ./install.sh --cwd /path/to/project                 # 默认 +5 小时
#   ./install.sh --cwd /path/to/project --offset +2H    # 自定义偏移（透传 date -v）
#   ./install.sh --uninstall                            # 卸载
#
# 原理: launchd 用户级 LaunchAgent + StartInterval 轮询，runner 自门控判断到点没。
#       【不用】StartCalendarInterval——实测它带 Year 键指向未来时刻会静默永不触发。
#       到点由 run-once.sh 发送，跑完自毁，不留常驻任务。

set -eu

LABEL="${TUI_LAUNCHD_LABEL:-com.user.tui-continue-once}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/run-once.sh"
SENDER="$(cd "$HERE/../.." && pwd)/scripts/tui-continue.sh"
LOG="/tmp/tui-continue-task.log"
DOMAIN="gui/$(id -u)"
TASK_DIR="$HOME/.tui-continue-task"
CONFIG="$TASK_DIR/config"

TARGET_CWD=""
OFFSET="+5H"
DO_UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) DO_UNINSTALL=1 ;;
    --cwd)       shift; [ $# -gt 0 ] || { echo "--cwd 缺少值"; exit 1; }; TARGET_CWD="$1" ;;
    --offset)    shift; [ $# -gt 0 ] || { echo "--offset 缺少值"; exit 1; }; OFFSET="$1" ;;
    -h|--help)   sed -n '2,13p' "$0"; exit 0 ;;
    *)           echo "未知参数: $1"; exit 1 ;;
  esac
  shift
done

# ---------- 卸载 ----------
if [ "$DO_UNINSTALL" = "1" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "已 bootout $LABEL" || echo "$LABEL 未加载"
  rm -f "$PLIST" && echo "已删除 $PLIST"
  rm -f "$CONFIG" && echo "已删除 $CONFIG"
  echo "（sentinel 与日志保留：$LOG / $LOG.fired）"
  exit 0
fi

[ -n "$TARGET_CWD" ] || { echo "ERROR: 必须指定 --cwd <项目路径>"; exit 1; }
TARGET_CWD=$(cd "$TARGET_CWD" 2>/dev/null && pwd -P) || { echo "ERROR: --cwd 路径不存在"; exit 1; }
[ -x "$RUNNER" ] || { echo "ERROR: $RUNNER 不可执行"; exit 1; }
[ -f "$SENDER" ] || { echo "ERROR: 找不到发送器 $SENDER"; exit 1; }
[ -d "/Applications/Ghostty.app" ] || { echo "ERROR: 没装 Ghostty"; exit 1; }

# ---------- 计算到点时刻 ----------
DUE_EPOCH=$(date -v"$OFFSET" +%s)
WHEN=$(date -r "$DUE_EPOCH" '+%Y-%m-%d %H:%M')
INTERVAL="${TUI_POLL_INTERVAL:-300}"     # 轮询间隔秒数

# ---------- 写配置（runner 读它判断到点没） ----------
mkdir -p "$TASK_DIR"
cat > "$CONFIG" <<EOF
TARGET_CWD=$TARGET_CWD
DUE_EPOCH=$DUE_EPOCH
EOF
echo "已写配置: $CONFIG"

# ---------- 生成 plist ----------
# 两个关键点（都实测踩过）：
#   1. 用 StartInterval 轮询 + runner 自门控，【不要】用 StartCalendarInterval。
#      LaunchAgent 的 StartCalendarInterval 带 Year 键指向未来时刻会静默永不触发
#      （runs=0、无日志、无报错）。详见 references/pitfalls.md 第 9 条。
#   2. ProgramArguments 显式走 /bin/sh <脚本>，【不要】直接把脚本当可执行文件。
#      直接执行实测会让 xpcproxy exit(78) EX_CONFIG，任务根本不起来。
#      详见 references/pitfalls.md 第 10 条。
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>$RUNNER</string>
	</array>
	<key>StartInterval</key>
	<integer>$INTERVAL</integer>
	<key>RunAtLoad</key>
	<false/>
	<key>StandardOutPath</key>
	<string>$LOG.launchd</string>
	<key>StandardErrorPath</key>
	<string>$LOG.launchd</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null || { echo "ERROR: plist 校验失败"; exit 1; }
echo "已生成 plist: $PLIST"
echo "目标对话 cwd: $TARGET_CWD"
echo "到点时刻: $WHEN（epoch $DUE_EPOCH）"
echo "轮询间隔: ${INTERVAL}s（每 ${INTERVAL}s 检查一次，到点才发送）"

# ---------- 加载 ----------
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "已加载: $LABEL"

# ---------- 自检 ----------
echo
echo "=== 注册状态（确认 StartInterval 生效）==="
launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "run interval|state|runs" | head -4
echo
echo "=== 目标自检（dry-run，不发送）==="
sh "$SENDER" --cwd "$TARGET_CWD" --dry-run
echo
echo "日志将写到: $LOG"
echo "卸载: $HERE/install.sh --uninstall"
