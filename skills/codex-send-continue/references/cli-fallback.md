# Codex CLI fallback

## 适用条件

不要用 AppleScript 模拟键盘向 ChatGPT 桌面端发消息：不可靠，`osascript rc=0` 不代表发送成功。

仅在原生投递工具不可用、目标与 CLI 支持情况已核实后使用。先检查目标是否存在活动 writer；不要为了尝试 CLI 主动关闭桌面应用。

锁冲突时的处置：

1. 若已证实目标被桌面端 writer 占用，说明关闭应用会影响其他任务；仅在用户授权后释放该占用。锁未冲突时无需关闭桌面端。
2. 确认锁已释放：
   ```bash
   ls -l ~/.codex/thread-writer-locks/<thread_id>.lock
   # 应显示 no such file / 不被占用
   ```
3. 进入该线程记录的 `cwd`（从 `session_meta` 的 `payload.cwd` 读取）。
4. 用官方 CLI 恢复并发送消息：
   ```bash
   cd <thread_cwd>
   codex exec resume <thread_id> "continue" --skip-git-repo-check --json \
     -o /tmp/codex_resume_last.txt
   ```

## 读取 thread_id 对应的 cwd

线程文件通常在：

```text
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread_id>.jsonl
```

第一行 `session_meta` 里有：

```json
{"payload": {"id": "...", "cwd": "/path/to/project", ...}}
```

## 验证是否成功

发送后检查线程文件是否新增了用户消息：

```bash
grep -n '"role":"user"' ~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl | tail
```

应能看到一条内容为 `continue` 的 `response_item`。

## 注意事项

- 如果 ChatGPT 桌面端正在运行，会报：
  ```text
  thread already has an active writer
  ```
  先核对占用者；不要删锁文件或抢写。只有用户授权关闭对应应用/会话后，才重试 CLI。
- 如果当前目录不是 Git 仓库或不在信任列表，需要加 `--skip-git-repo-check`。
- 如果账号 usage limit 未恢复，CLI 会报 `usage limit`，这是额度问题，不是通道问题；等额度恢复后重试即可。
- 这是历史验证过的 fallback。当前版本须以实际 CLI 帮助与返回结果为准；消息被接受不等于任务完成。
