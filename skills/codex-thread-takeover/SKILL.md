---
name: codex-thread-takeover
description: >
  读取一个指定的本地 Codex 对话线程（输入形如 `codex://threads/<uuid>` 或裸
  UUID），还原它的完整上下文——原始目标、做过的事、改过的文件、最后一步的
  状态（完成/失败/中断及原因）——然后由 Claude Code 接管，在当前会话里把
  剩下的工作继续做下去。用于「这个 codex 线程我想换 Claude 接着做」「读一下
  这个 codex thread」「接管这个 codex 会话」之类的场景。只读，不会启动/写
  Codex 的任何会话。
---

# Codex Thread Takeover

把一个本地 Codex（CLI / Desktop）线程的完整上下文读出来，交给 Claude Code
接手继续做。**这不是 `codex resume`（不会打开 Codex TUI），也不是 `codex
queue`（不会把消息塞回原 Codex 会话）** —— 是把那个线程的历史当成本次任务的
背景，由 Claude Code 自己的工具（Read/Edit/Bash/…）在当前会话里继续干。

## 输入

用户会给一个类似 `codex://threads/01a05ab5-c175-7421-a9fc-5cef1f8d243c` 的
标识，或者裸 UUID。两种都支持，脚本会自己从输入里抠出 UUID。

## 步骤

1. 跑脚本拿结构化数据（只读，几十到几百毫秒量级，不用担心性能）：

   ```
   python3 ~/.claude/skills/codex-thread-takeover/scripts/read_thread.py "<用户给的 codex://threads/... 或裸 UUID>"
   ```

   脚本失败会给出明确 JSON `{"error": ...}`（找不到线程 / 不是合法 UUID /
   CODEX_HOME 不存在）——照实转达给用户，不要猜测或编造线程内容。

2. 读输出 JSON，理解关键字段：
   - `fidelity`: `"sqlite"`（主路径，完整保真）或 `"best_effort_legacy"`
     （极老、未被 Codex 迁移进分页索引的线程，走 jsonl 兜底解析，部分元数据
     如 git_branch/git_sha 可能是 `null`）。看到 `best_effort_legacy` 时要
     在给用户的简报里如实标注「这部分保真度较低」，不要当成和主路径一样可靠。
   - `meta`: cwd / git_branch / git_sha / model / 创建更新时间 / 是否 archived。
   - `last_turn` 和 `failed_or_error_turns`: 最后一轮的状态
     (`completed`/`failed`/`inProgress`/…)，失败时 `error` 字段里通常有具体
     原因（比如用量超限、工具报错）——这是判断"为什么需要接管"的关键线索。
   - `last_plan`: 该线程最后一次更新的计划/TODO（如果有），代表 Codex 那边
     认为"接下来该做什么"。
   - `files_touched`: 这个线程动过的文件列表（路径 + 增/改/删），用来判断
     接管范围。
   - `timeline`: 按时间顺序的事件（user 消息、agent 回复、命令执行、文件改
     动、reasoning 摘要……）。噪声类型（commandExecution/reasoning/…）默认
     只保留最近 N 条（`--max-items-per-type`，默认 60；narrative 类型
     user/agent/plan 保留 3 倍）。如果关键信息像是被截断了，用
     `--full`（不设上限）或 `--raw-ordinal <n>`（拿某一条的完整原始
     `item_json`）重新跑脚本，不要靠猜测补全。
   - `env_check`: 当前工作区和线程记录时的状态是否一致——`cwd_exists` /
     `is_git_repo` / `current_branch` vs `recorded_branch` /
     `current_head_sha` vs `recorded_sha` / `worktree_dirty`。**这是能不能
     直接接的关键**：分支不存在了、HEAD 已经变了、或者工作区是脏的，都说明
     现场和线程记录的不一样，必须先跟用户对齐，不能自作主张
     reset/checkout/stash。

3. 给用户一份「接管简报」（原生对话里说清楚即可，不必生成新文档），至少覆盖：
   - 这个线程原本要做什么（首条用户消息 / 标题）
   - 已经做了什么、改了哪些文件、跑过什么关键命令
   - 停在哪一步、为什么停（成功收尾 / 失败原因 / 还在 inProgress）
   - 环境现实核对的结论：能不能直接在当前 cwd 接着做，还是要先处理分支/
     脏工作区的落差

4. 按本项目 `CLAUDE.md` 的任务规范继续：非 trivial 的后续实现要先建立任务
   契约（Mode/Goal/Scope/Base SHA/验收标准），复杂改动进入 plan mode 或先
   过一遍 Direction Check——不要因为"是接管别人的活"就跳过这些确认直接开始
   改代码。用户对这份简报的确认，等同于新任务的起点，不等同于自动授权执行
   该线程里记录的所有后续计划。

## 已知限制（如实告诉用户，不要藏）

- 依赖 `~/.codex/state_5.sqlite`（`threads` 表）和
  `~/.codex/thread_history_1.sqlite`（`thread_items`/`thread_turns` 表）这两
  个**未公开、Codex 内部私有**的格式；本 skill 在 Codex CLI 0.153.4 下验证
  通过。Codex 升级后这两个 schema 可能变化——脚本读失败或字段对不上时如实
  报错/报告，不要硬凑内容掩盖。
- 全程只读：绝不写 `~/.codex` 下任何文件，也不会调用 `codex` CLI 本身。
- `CODEX_HOME` 环境变量会被尊重（对应 Codex 自己的约定），未设置时默认
  `~/.codex`。
- Desktop 端悬浮实时写入的会话理论上可能触发一次 `database is locked`；脚本
  已内置短重试，仍失败会在 JSON 里报错。
