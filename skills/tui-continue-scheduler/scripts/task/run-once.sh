#!/bin/sh
# run-once.sh —— 定时任务执行体（由 launchd 每 5 分钟调用一次，到点才真正发送）
#
# 为什么是"轮询 + 自门控"而不是 StartCalendarInterval：
#   实测踩到 macOS 的坑——LaunchAgent 用 StartCalendarInterval（带 Year 键）指向未来某一时刻，
#   launchd 会【静默地永不触发】：runs=0、无日志、无报错，任务看起来装好了其实什么都没发生。
#   改用 StartInterval=300 每 5 分钟醒一次，脚本自己判断"到点没"，到点才发送并自毁。
#   这样也天然免疫睡眠/唤醒：唤醒后下一个 interval 自然会补上。
#
# 目标与到点时间从配置文件读（install.sh 写入），不用 plist 的 EnvironmentVariables：
#   $TASK_DIR/config   KEY=VALUE 形式，含 TARGET_CWD 与 DUE_EPOCH

set -u

TASK_DIR="${TUI_TASK_DIR:-$HOME/.tui-continue-task}"
LOG="${TUI_LOG:-/tmp/tui-continue-task.log}"
SENTINEL="$LOG.fired"
LABEL="${TUI_LAUNCHD_LABEL:-com.user.tui-continue-once}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG="$TASK_DIR/config"
HERE="$(cd "$(dirname "$0")" && pwd)"
SENDER="${TUI_SENDER:-$(cd "$HERE/.." && pwd)/tui-continue.sh}"

# ---------- 读配置 ----------
[ -f "$CONFIG" ] || exit 0            # 没配置就静默退出（可能已被手工清理）
TARGET_CWD=""
DUE_EPOCH=""
while IFS='=' read -r k v || [ -n "$k" ]; do
  case "$k" in
    TARGET_CWD) TARGET_CWD="$v" ;;
    DUE_EPOCH)  DUE_EPOCH="$v" ;;
  esac
done < "$CONFIG"

[ -n "$TARGET_CWD" ] || exit 0
[ -n "$DUE_EPOCH" ] || exit 0

# ---------- 自门控：没到点就静默退出 ----------
NOW=$(date +%s)
if [ "$NOW" -lt "$DUE_EPOCH" ]; then
  exit 0                              # 不写日志，避免每 5 分钟刷屏
fi

# ---------- 到点了：执行 ----------
{
  echo "=================================================="
  echo "[$(date '+%F %T')] 到点触发（应触发于 $(date -r "$DUE_EPOCH" '+%F %T')，uid=$(id -u)）"

  # 防重复：正常跑过就留 sentinel，之后即使 agent 被重新加载也不会再发
  if [ -f "$SENTINEL" ]; then
    echo "[$(date '+%F %T')] sentinel 已存在（$(cat "$SENTINEL" 2>/dev/null)），跳过"
    exit 0
  fi

  STATUS="?"
  if [ ! -f "$SENDER" ]; then
    echo "[$(date '+%F %T')] ERROR: 找不到发送器 $SENDER"
    STATUS=127
  else
    sh "$SENDER" --cwd "$TARGET_CWD"
    STATUS=$?
  fi

  case "$STATUS" in
    0) echo "[$(date '+%F %T')] 结论: 成功，消息已确认送达并落库" ;;
    1) echo "[$(date '+%F %T')] 结论: 参数/环境错误，未发送" ;;
    2) echo "[$(date '+%F %T')] 结论: 没找到目标（进程或面板不在了）——未发送任何消息" ;;
    3) echo "[$(date '+%F %T')] 结论: osascript 注入失败" ;;
    4) echo "[$(date '+%F %T')] 结论: 已注入但未确认落库，需人工检查该对话" ;;
    *) echo "[$(date '+%F %T')] 结论: 异常退出码 $STATUS" ;;
  esac

  date '+%F %T' > "$SENTINEL"

  # ---- 自毁：一次性任务不留常驻 agent ----
  echo "[$(date '+%F %T')] 自毁中: bootout $LABEL"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>&1 || echo "  bootout 失败（可能已卸载）"
  [ -f "$PLIST" ] && rm -f "$PLIST" && echo "  已删除 $PLIST"
  [ -f "$CONFIG" ] && rm -f "$CONFIG" && echo "  已删除 $CONFIG"
  echo "[$(date '+%F %T')] 任务结束"
} >> "$LOG" 2>&1
