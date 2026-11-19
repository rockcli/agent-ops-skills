#!/bin/sh
# tui-continue.sh —— 向 Ghostty 里运行的交互式 agent TUI（典型：Claude Code）注入一条文本并回车，
# 然后回读 session 日志验证消息真的落库。
#
# 用法:
#   tui-continue.sh --cwd <项目路径> [--dry-run] [--text "continue"]
#                   [--session <路径>] [--terminal <UUID>] [--expect-user-regex <RE>]
#
#   --cwd 必填（或设 TUI_TARGET_CWD 环境变量）。它是"哪个对话"的判定依据。
#
# 退出码: 0=成功落库  1=参数/环境错误  2=定位歧义或找不到目标
#         3=osascript 发送失败  4=发送了但未确认落库（需人工检查）
#
# 硬约束（都踩过，别改）:
#   - input text 与 send key "enter" 分两次 osascript 调用，间隔 >=2s
#   - 会话身份取 session 文件里【第一条】cwd，不是最后一条（sidechain 会污染）
#   - 纯 POSIX sh：本机非交互 shell 是 zsh，bash 专属语法会炸
#   - 不硬编码 terminal UUID，运行时枚举

set -u

# ---------- 默认配置 ----------
TARGET_CWD="${TUI_TARGET_CWD:-}"
SESSION_FILE=""
TERMINAL_ID=""
TEXT="continue"
DRY_RUN=0
EXPECT_RE=""
ENTER_DELAY="${TUI_ENTER_DELAY:-3}"      # 必须 >=2
VERIFY_TIMEOUT="${TUI_VERIFY_TIMEOUT:-60}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*"; exit "${2:-1}"; }

# ---------- 参数解析 ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)           DRY_RUN=1 ;;
    --text)              shift; [ $# -gt 0 ] || die "--text 缺少值" 1; TEXT="$1" ;;
    --cwd)               shift; [ $# -gt 0 ] || die "--cwd 缺少值" 1; TARGET_CWD="$1" ;;
    --session)           shift; [ $# -gt 0 ] || die "--session 缺少值" 1; SESSION_FILE="$1" ;;
    --terminal)          shift; [ $# -gt 0 ] || die "--terminal 缺少值" 1; TERMINAL_ID="$1" ;;
    --expect-user-regex) shift; [ $# -gt 0 ] || die "--expect-user-regex 缺少值" 1; EXPECT_RE="$1" ;;
    -h|--help)           sed -n '2,17p' "$0"; exit 0 ;;
    *)                   die "未知参数: $1" 1 ;;
  esac
  shift
done

[ -n "$TARGET_CWD" ] || die "必须指定 --cwd <项目路径>（或设 TUI_TARGET_CWD）" 1
command -v osascript >/dev/null 2>&1 || die "找不到 osascript" 1
[ -d "/Applications/Ghostty.app" ] || die "Ghostty 未安装" 1

# ---------- 1. 定位目标 agent 进程（只要一个，多个就拒绝） ----------
CANDIDATES=""
for p in $(pgrep -x claude 2>/dev/null); do
  # 必须有控制终端：headless 的 tty 为空或 "?" / "??"
  tty=$(ps -o tty= -p "$p" 2>/dev/null | tr -d ' ')
  case "$tty" in
    ""|"?"|"??") continue ;;
  esac
  # 排除 headless（--print / -p）
  cmd=$(ps -o command= -p "$p" 2>/dev/null)
  if printf '%s' "$cmd" | grep -qE '(^| )(--print|-p)( |$)'; then
    continue
  fi
  CANDIDATES="$CANDIDATES$p
"
done
CANDIDATES=$(printf '%s' "$CANDIDATES" | grep . || true)

[ -n "$CANDIDATES" ] || die "没有正在运行的交互式 Claude Code 进程" 2
N_PIDS=$(printf '%s\n' "$CANDIDATES" | grep -c .)
if [ "$N_PIDS" -gt 1 ]; then
  log "发现 $N_PIDS 个交互式 Claude Code 进程，无法安全判断目标："
  for p in $CANDIDATES; do
    log "  pid=$p tty=$(ps -o tty= -p "$p" 2>/dev/null | tr -d ' ') cwd=$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  done
  die "请用 --terminal <UUID> 或 --session <路径> 明确指定目标" 2
fi
CLAUDE_PID=$(printf '%s\n' "$CANDIDATES" | head -1)
log "目标进程: pid=$CLAUDE_PID tty=$(ps -o tty= -p "$CLAUDE_PID" | tr -d ' ') etime=$(ps -o etime= -p "$CLAUDE_PID" | tr -d ' ')"

CLAUDE_CWD=$(lsof -a -p "$CLAUDE_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
[ -n "$CLAUDE_CWD" ] || CLAUDE_CWD="$TARGET_CWD"
CLAUDE_CWD=$(cd "$CLAUDE_CWD" 2>/dev/null && pwd -P) || die "无法解析进程 cwd" 2
log "进程 cwd: $CLAUDE_CWD"

# 目标 cwd 与进程 cwd 必须一致，否则说明指定的不是同一个对话
TARGET_CWD=$(cd "$TARGET_CWD" 2>/dev/null && pwd -P) || die "--cwd 路径不存在: $TARGET_CWD" 1
if [ "$CLAUDE_CWD" != "$TARGET_CWD" ]; then
  die "进程 cwd ($CLAUDE_CWD) 与 --cwd ($TARGET_CWD) 不一致，拒绝发送" 2
fi

# ---------- 2. 定位 session 日志（会话身份 = 第一条 cwd） ----------
if [ -z "$SESSION_FILE" ]; then
  # 坑：同一 session 文件里 cwd 会出现多个值——子 agent（sidechain）会写入自己的 cwd。
  # 取"最后一条 cwd"会让主会话被误判为不匹配、整条丢弃，进而选中已闲置的旧会话。
  RESOLVER='
import sys, os, glob, json
target = sys.argv[1]
root = os.path.expanduser("~/.claude/projects")
hits = []
for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
    first_cwd = ""
    try:
        with open(f, errors="ignore") as fh:
            for line in fh:
                try: d = json.loads(line)
                except Exception: continue
                c = d.get("cwd")
                if c:                      # 第一条带 cwd 的记录 = 会话身份
                    first_cwd = c
                    break
    except OSError:
        continue
    if first_cwd != target:
        continue
    hits.append((os.path.getmtime(f), f))
if not hits:
    print(json.dumps({"status": "none"})); sys.exit(0)
hits.sort(reverse=True)
top_m, top_f = hits[0]
if len(hits) > 1 and (top_m - hits[1][0]) < 120:
    print(json.dumps({"status": "ambiguous", "a": top_f, "b": hits[1][1]})); sys.exit(0)
print(json.dumps({"status": "ok", "path": top_f, "count": len(hits)}))
'
  RESOLVED=$(python3 -c "$RESOLVER" "$CLAUDE_CWD" 2>/dev/null)
  [ -n "$RESOLVED" ] || die "session 解析脚本执行失败" 2
  R_STATUS=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  case "$R_STATUS" in
    ok)
      SESSION_FILE=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin)["path"])')
      N_CAND=$(printf '%s' "$RESOLVED" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count",1))')
      [ "$N_CAND" -gt 1 ] && log "该 cwd 下有 $N_CAND 个历史会话，取最新的一个"
      ;;
    ambiguous)
      log "两个候选会话更新时间过于接近（<120s），无法安全判断哪个是活动会话："
      printf '%s' "$RESOLVED" | python3 -c 'import sys,json,os
d=json.load(sys.stdin)
print("  " + os.path.basename(d["a"]))
print("  " + os.path.basename(d["b"]))'
      die "拒绝猜测。请显式传 --session <路径>" 2
      ;;
    *)
      die "找不到 cwd=$CLAUDE_CWD 对应的 session 日志" 2
      ;;
  esac
fi
[ -f "$SESSION_FILE" ] || die "session 文件不存在: $SESSION_FILE" 2
log "目标 session: $(basename "$SESSION_FILE")"

# ---------- 3. 定位 Ghostty 面板 ----------
if [ -z "$TERMINAL_ID" ]; then
  # 注意：AppleScript 的 `tab` 是字面量字符串 "tab"，不是制表符，所以用 ||| 分隔
  TM_LIST=$(osascript -e 'tell application "Ghostty"
set out to ""
repeat with w from 1 to (count of windows)
repeat with t from 1 to (count of tabs of window w)
repeat with tm in terminals of tab t of window w
set out to out & (id of tm) & "|||" & (working directory of tm) & linefeed
end repeat
end repeat
end repeat
return out
end tell' 2>/dev/null)
  [ -n "$TM_LIST" ] || die "无法枚举 Ghostty 面板" 2
  HIT=$(printf '%s\n' "$TM_LIST" | awk -F'\\|\\|\\|' -v cwd="$CLAUDE_CWD" '$2==cwd {print $1}')
  [ -n "$HIT" ] || die "Ghostty 里没有 cwd=$CLAUDE_CWD 的面板（有人在面板里 cd 过？）" 2
  N_HIT=$(printf '%s\n' "$HIT" | grep -c .)
  if [ "$N_HIT" -gt 1 ]; then
    log "该 cwd 下有 $N_HIT 个面板，取第一个："
    printf '%s\n' "$HIT" | sed 's/^/  /'
  fi
  TERMINAL_ID=$(printf '%s\n' "$HIT" | head -1)
fi
log "目标面板: $TERMINAL_ID"

# ---------- 4. 发送 ----------
SIZE_BEFORE=$(stat -f %z "$SESSION_FILE")
LINES_BEFORE=$(wc -l < "$SESSION_FILE" | tr -d ' ')
log "发送前: size=$SIZE_BEFORE lines=$LINES_BEFORE mtime=$(stat -f %Sm "$SESSION_FILE")"

if [ "$DRY_RUN" = "1" ]; then
  log "DRY-RUN：跳过注入。将执行："
  log "  input text '$TEXT' -> terminal id $TERMINAL_ID"
  log "  sleep $ENTER_DELAY; send key enter -> terminal id $TERMINAL_ID"
  exit 0
fi

TEXT_JSON=$(printf '%s' "$TEXT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
osascript -e "tell application \"Ghostty\" to input text $TEXT_JSON to (terminal id \"$TERMINAL_ID\")" \
  || die "input text 失败" 3
sleep "$ENTER_DELAY"
osascript -e "tell application \"Ghostty\" to send key \"enter\" to (terminal id \"$TERMINAL_ID\")" \
  || die "send key enter 失败" 3
log "已发送: '$TEXT' + enter（间隔 ${ENTER_DELAY}s）"

# ---------- 5. 回读验证：jsonl 尾部必须新增匹配的 user 记录 ----------
python3 -c '
import sys, json, time, os, re
path, before, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
expect_re, sent_text = sys.argv[4], sys.argv[5]
rx = re.compile(expect_re) if expect_re else None
deadline = time.time() + timeout
found = None
while time.time() < deadline and not found:
    try:
        if os.path.getsize(path) > before:
            with open(path, errors="ignore") as f:
                f.seek(before)
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: d = json.loads(line)
                    except Exception: continue
                    m = d.get("message") or {}
                    if not isinstance(m, dict) or m.get("role") != "user": continue
                    c = m.get("content")
                    if isinstance(c, str): txt = c
                    elif isinstance(c, list):
                        txt = " ".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
                    else: txt = ""
                    if not txt.strip(): continue
                    if rx:
                        if not rx.search(txt): continue
                    elif sent_text.strip() and sent_text.strip() not in txt:
                        continue
                    found = (d.get("timestamp"), txt.strip()[:120]); break
    except FileNotFoundError:
        pass
    if not found: time.sleep(1.5)
if found:
    print("VERIFIED ts=%s text=%r" % found); sys.exit(0)
print("NOT-VERIFIED: %ss 内未在 jsonl 尾部找到匹配的 user 记录" % timeout)
sys.exit(4)
' "$SESSION_FILE" "$SIZE_BEFORE" "$VERIFY_TIMEOUT" "$EXPECT_RE" "$TEXT"
RC=$?

LINES_AFTER=$(wc -l < "$SESSION_FILE" | tr -d ' ')
log "发送后: size=$(stat -f %z "$SESSION_FILE") lines=$LINES_AFTER"
if [ "$RC" = "0" ]; then
  log "SUCCESS：消息已确认落到 session，TUI 已收到 '$TEXT'"
else
  log "WARNING：消息未能确认落库（rc=$RC），请人工检查该对话"
fi
exit "$RC"
