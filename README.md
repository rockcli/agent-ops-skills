<div align="center">

# agent-ops-skills

**A curated pack of 16 battle-tested Agent Skills for multi-model blind review, cross-host orchestration, and terminal automation.**

[![Skills](https://img.shields.io/badge/Skills-16-111827)](skills)
[![Agents](https://img.shields.io/badge/Agents-Claude%20Code%20%7C%20Codex%20%7C%20Antigravity%20%7C%20DSH-2563EB)](#quick-start)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

</div>

## Features

- ⚖️ **Unbiased multi-model review** — Run independent Codex + Claude Opus blind reviews in parallel on frozen Git/working-tree snapshots, with automatic DeepSeek Harness (`dsh`) fallback or optional 3rd-opinion arbitration.
- 🔁 **Cross-host session handoff** — Read local Codex threads (`codex://threads/<uuid>`) into Claude Code, bridge existing `dsh` sessions, or schedule verified text injection into running Ghostty TUI sessions.
- 🛡️ **Safety-first ops guardrails** — PID ownership checks, read-only disk analysis with Trash-only reversible cleanup, explicit write/authorization gates, and anti-overengineering playbooks.
- 🧪 **Zero-dependency & tested** — Pure Python 3 / POSIX sh / Node 18 scripts with 51 local unit tests and zero external packages required.

## Skills

### Multi-Model Independent Review & Delegation

| Skill | Summary | Runtime |
|-------|---------|---------|
| [`codex-review`](skills/codex-review/SKILL.md) | Independent read-only review via Codex (`CODEX_REVIEW_MODEL`, default `gpt-5.4`) with frozen snapshots, PID checks, and dual-review coordination | Python 3.12+ (`scripts/codex.py` + 14 tests) |
| [`codex-task`](skills/codex-task/SKILL.md) | Cross-host task delegation to Codex for read-only investigation or authorized worktree edits | Reuses `codex-review/scripts/codex.py` |
| [`opus-review`](skills/opus-review/SKILL.md) | Independent blind review via Claude Opus (`medium`/`high` effort) with durable job state and notification deduplication | Python 3.12+ (`scripts/opus.py` + 10 tests) |
| [`opus-task`](skills/opus-task/SKILL.md) | Cross-host task delegation to Claude Code / Opus with explicit tool and authorization boundaries | Reuses `opus-review/scripts/opus.py` |
| [`dsh-review`](skills/dsh-review/SKILL.md) | Independent read-only review via DeepSeek Harness (`dsh`), acting as automatic fallback or optional 3rd opinion | Python 3.12+ (`scripts/dsh.py` + 19 tests) |
| [`tri-council`](skills/tri-council/SKILL.md) | Unbiased multi-model co-design, anti-overengineering audit, and parallel worktree A/B implementation ("racing") | SOP |

### Cross-Host Handoff & Terminal Automation

| Skill | Summary | Runtime |
|-------|---------|---------|
| [`codex-thread-takeover`](skills/codex-thread-takeover/SKILL.md) | Read-only extractor for local Codex CLI/Desktop threads (`codex://threads/<uuid>`) so Claude Code can seamlessly continue the task | Python 3 (`scripts/read_thread.py`) |
| [`codex-send-continue`](skills/codex-send-continue/SKILL.md) | Continue an existing Codex task with a follow-up message via native tools or CLI fallback | SOP + CLI reference |
| [`dsh-session-bridge`](skills/dsh-session-bridge/SKILL.md) | Locate, read, search, steer, queue, answer questions for, and hand off existing `dsh` sessions across hosts | Node 18+ (`scripts/dsh-session.mjs`) |
| [`tui-continue-scheduler`](skills/tui-continue-scheduler/SKILL.md) | Schedule one-shot delayed text injection into a running Ghostty TUI session (e.g. Claude Code) with session-log delivery proof | POSIX sh + macOS `launchd` + `pitfalls.md` |
| [`ghostty-multi-agent-orchestration`](skills/ghostty-multi-agent-orchestration/SKILL.md) | Playbook for orchestrating multiple CLI agents (`claude`, `codex`, `kimi`, `pi`) in Ghostty panels or headless mode safely | SOP |
| [`agent-watchdog-advisor`](skills/agent-watchdog-advisor/SKILL.md) | Proactive periodic monitoring for long-running tasks or external agent sessions with ETA, confidence, and unstuck diagnosis | SOP |

### Developer & Engineering Workflows

| Skill | Summary | Runtime |
|-------|---------|---------|
| [`mr-review-mail`](skills/mr-review-mail/SKILL.md) | GitLab MR review with strict new-vs-legacy attribution, separating defect comments from concise engineering-principle emails | Python 3 (`scripts/send_mail.py` + 8 tests) |
| [`storage-analyzer`](skills/storage-analyzer/SKILL.md) | Read-only macOS/Windows disk analyzer with 3-tier safety classification, interactive HTML report, and reversible Trash-only web UI | Python 3 (`scripts/scan.py`, `server.py`) |
| [`web-interaction-design`](skills/web-interaction-design/SKILL.md) | Systematic Web UI/UX redesign workflow: heuristic audit → CSS design tokens → component states → screenshot verification | SOP |
| [`colab-operator`](skills/colab-operator/SKILL.md) | Operate Google Colab GPU/TPU sessions (`H100`, `A100`, `L4`, `T4`, `v5e1`, `v6e1`) via `google-colab-cli` | CLI guide |

## Quick Start

```bash
git clone https://github.com/rockcli/agent-ops-skills.git
cd agent-ops-skills
./scripts/install.sh
```

`./scripts/install.sh` symlinks all 16 skills into `~/.agents/skills` and **skips existing entries by default** so your local customizations are never overwritten.

**Selective or custom install:**

```bash
# Install specific skills only
./scripts/install.sh codex-thread-takeover tui-continue-scheduler web-interaction-design

# Install into ~/.claude/skills or ~/.codex/skills
./scripts/install.sh --target ~/.claude/skills

# Copy instead of symlinking (add --force to replace existing entries)
./scripts/install.sh --copy
```

## Configuration & Testing

- **`CODEX_REVIEW_MODEL`** — Override the default Codex model (`gpt-5.4`) in `codex-review/scripts/codex.py` (or pass `--model` per call).
- **`TUI_LAUNCHD_LABEL`** — Override the macOS `launchd` label (`com.user.tui-continue-once`) in `tui-continue-scheduler`.

```bash
python3 -m unittest discover -s skills/codex-review/scripts/tests -v
python3 -m unittest discover -s skills/opus-review/scripts/tests -v
python3 -m unittest discover -s skills/dsh-review/scripts/tests -v
python3 -m unittest discover -s skills/mr-review-mail/scripts -p 'test_*.py' -v
```

## Attribution & Notes

- All skills run locally with your own CLI/tool credentials and zero telemetry.
- `skills/opus-review/scripts/vendor/aris_review.py` is vendored from [ARIS Claude Review MCP](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep) under the MIT License; see [`provenance.md`](skills/opus-review/references/provenance.md) and [`vendor/LICENSE`](skills/opus-review/scripts/vendor/LICENSE).

## License

[MIT](LICENSE) © [rockcli](https://github.com/rockcli)
