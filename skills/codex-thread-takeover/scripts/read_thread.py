#!/usr/bin/env python3
"""Read a local Codex CLI/Desktop conversation thread and dump a structured,
capped JSON summary for another agent (Claude Code) to take the work over.

READ-ONLY. Never writes to anything under $CODEX_HOME.

Data sources (undocumented, private Codex internals -- may break across
Codex versions; this script fails loudly rather than guessing):

  $CODEX_HOME/state_5.sqlite            table `threads`       -- thread metadata
  $CODEX_HOME/thread_history_1.sqlite   table `thread_items`  -- normalized,
                                          paginated conversation items (primary path)
  $CODEX_HOME/thread_history_1.sqlite   table `thread_turns`  -- per-turn status/errors
  $CODEX_HOME/sessions/**/*.jsonl       rollout files          -- legacy fallback for
  $CODEX_HOME/archived_sessions/*.jsonl                          threads never migrated
                                                                  into thread_history_1.sqlite

Usage:
  read_thread.py <thread_id_or_codex_uri> [--codex-home DIR]
                 [--max-items-per-type N] [--full] [--raw-ordinal N]
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

DEFAULT_MAX_ITEMS_PER_TYPE = 60
MAX_TEXT = 4000
MAX_PLAN_TEXT = 8000
MAX_DIFF = 1500
MAX_CMD_FIELD = 400
MAX_GENERIC = 800

# PascalCase (legacy jsonl `item_completed.item.type`) -> camelCase
# (thread_items.item_type in the sqlite index). Kept explicit rather than a
# naive lowercase-first-letter rule because some types (McpToolCall) do not
# follow that rule.
LEGACY_TYPE_MAP = {
    "UserMessage": "userMessage",
    "AgentMessage": "agentMessage",
    "Reasoning": "reasoning",
    "CommandExecution": "commandExecution",
    "FileChange": "fileChange",
    "McpToolCall": "mcpToolCall",
    "SubAgentActivity": "subAgentActivity",
    "WebSearch": "webSearch",
    "ContextCompaction": "contextCompaction",
    "CollabAgentToolCall": "collabAgentToolCall",
    "ImageView": "imageView",
    "Plan": "plan",
    "FunctionCallOutput": "functionCallOutput",
    "EnteredReviewMode": "enteredReviewMode",
    "ExitedReviewMode": "exitedReviewMode",
    "ImageGeneration": "imageGeneration",
    "DynamicToolCall": "dynamicToolCall",
}


def cap(s, n):
    if s is None:
        return None
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"...[+{len(s) - n} chars truncated, rerun with --full or --raw-ordinal]"


def extract_thread_id(raw):
    raw = raw.strip()
    m = UUID_RE.search(raw)
    if not m:
        return None
    return m.group(0)


def codex_home(arg):
    if arg:
        return os.path.expanduser(arg)
    return os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


def open_ro(path, retries=5, delay=0.3):
    uri = f"file:{path}?mode=ro"
    last_err = None
    for _ in range(retries):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(f"could not open {path} read-only after {retries} tries: {last_err}")


def load_thread_meta(home, thread_id):
    path = os.path.join(home, "state_5.sqlite")
    if not os.path.exists(path):
        return None
    conn = open_ro(path)
    try:
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def load_paginated_items(home, thread_id):
    path = os.path.join(home, "thread_history_1.sqlite")
    if not os.path.exists(path):
        return []
    conn = open_ro(path)
    try:
        rows = conn.execute(
            "SELECT item_id, item_type, rollout_ordinal, created_at_ms, item_json "
            "FROM thread_items WHERE thread_id = ? ORDER BY rollout_ordinal ASC",
            (thread_id,),
        ).fetchall()
        items = []
        for r in rows:
            try:
                obj = json.loads(r["item_json"])
            except json.JSONDecodeError:
                obj = {"_unparsed": r["item_json"]}
            items.append(
                {
                    "ordinal": r["rollout_ordinal"],
                    "item_type": r["item_type"],
                    "created_at_ms": r["created_at_ms"],
                    "obj": obj,
                }
            )
        return items
    finally:
        conn.close()


def load_turns(home, thread_id):
    path = os.path.join(home, "thread_history_1.sqlite")
    if not os.path.exists(path):
        return []
    conn = open_ro(path)
    try:
        rows = conn.execute(
            "SELECT turn_id, status, error_json, started_at, completed_at, duration_ms "
            "FROM thread_turns WHERE thread_id = ? ORDER BY started_at ASC",
            (thread_id,),
        ).fetchall()
        out = []
        for r in rows:
            err = None
            if r["error_json"]:
                try:
                    err = json.loads(r["error_json"])
                except json.JSONDecodeError:
                    err = r["error_json"]
            out.append(
                {
                    "turn_id": r["turn_id"],
                    "status": r["status"],
                    "error": err,
                    "started_at": r["started_at"],
                    "completed_at": r["completed_at"],
                    "duration_ms": r["duration_ms"],
                }
            )
        return out
    finally:
        conn.close()


def find_rollout_path(home, thread_id, recorded_path):
    if recorded_path and os.path.exists(recorded_path):
        return recorded_path
    for base in ("sessions", "archived_sessions"):
        pattern = os.path.join(home, base, "**", f"*{thread_id}.jsonl")
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return None


def load_legacy_items(rollout_path):
    """Best-effort fallback for threads not (yet) indexed into
    thread_history_1.sqlite. Field names inside each item are legacy
    snake_case/PascalCase and are NOT normalized to the sqlite convention --
    callers must treat this as reduced-fidelity ('fidelity': 'best_effort_legacy')."""
    items = []
    meta = {}
    if not rollout_path or not os.path.exists(rollout_path):
        return items, meta
    with open(rollout_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            payload = d.get("payload") or {}
            if t == "session_meta" and not meta:
                meta = {
                    "cwd": payload.get("cwd"),
                    "originator": payload.get("originator"),
                    "cli_version": payload.get("cli_version"),
                    "source": payload.get("source"),
                }
            elif t == "turn_context" and "workspace_roots" not in meta:
                meta["workspace_roots"] = payload.get("workspace_roots")
                meta["approval_policy"] = payload.get("approval_policy")
                meta["sandbox_policy"] = payload.get("sandbox_policy")
            elif t == "event_msg" and payload.get("type") == "item_completed":
                item = payload.get("item") or {}
                raw_type = item.get("type", "Unknown")
                item_type = LEGACY_TYPE_MAP.get(raw_type, raw_type)
                items.append(
                    {
                        "ordinal": d.get("ordinal"),
                        "item_type": item_type,
                        "created_at_ms": payload.get("completed_at_ms") or payload.get("started_at_ms"),
                        "obj": item,
                        "_legacy_raw_type": raw_type,
                    }
                )
            elif t == "event_msg" and payload.get("type") == "turn_aborted":
                items.append(
                    {
                        "ordinal": d.get("ordinal"),
                        "item_type": "turnAborted",
                        "created_at_ms": None,
                        "obj": payload,
                    }
                )
    return items, meta


def text_from_content(content):
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict):
            if c.get("type") in ("text", "input_text", "output_text") and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts)


def summarize_item_sqlite(item_type, obj):
    """Extract (summary, detail) for a normalized (sqlite-convention) item."""
    if item_type == "userMessage":
        text = text_from_content(obj.get("content"))
        return "user: " + cap(text, 200), cap(text, MAX_TEXT)
    if item_type == "agentMessage":
        text = obj.get("text", "")
        phase = obj.get("phase")
        return f"agent[{phase}]: " + cap(text, 200), cap(text, MAX_TEXT)
    if item_type == "reasoning":
        summary = obj.get("summary") or []
        joined = " / ".join(summary) if isinstance(summary, list) else str(summary)
        return "reasoning: " + cap(joined, 200), cap(joined, MAX_TEXT)
    if item_type == "commandExecution":
        command = obj.get("command", "")
        status = obj.get("status")
        cwd = obj.get("cwd")
        out = obj.get("aggregatedOutput", "")
        return (
            f"cmd[{status}]: " + cap(command, 200),
            json.dumps(
                {
                    "command": cap(command, MAX_CMD_FIELD),
                    "cwd": cwd,
                    "status": status,
                    "output": cap(out, MAX_CMD_FIELD),
                },
                ensure_ascii=False,
            ),
        )
    if item_type == "fileChange":
        changes = obj.get("changes") or []
        paths = []
        details = []
        for c in changes:
            path = c.get("path")
            kind = (c.get("kind") or {}).get("type")
            paths.append(f"{kind}:{path}")
            details.append(
                {
                    "path": path,
                    "kind": kind,
                    "diff": cap(c.get("diff"), MAX_DIFF),
                }
            )
        return "filechange: " + cap(", ".join(paths), 300), json.dumps(details, ensure_ascii=False)
    if item_type == "plan":
        text = obj.get("text", "")
        return "plan update (" + str(len(text)) + " chars)", cap(text, MAX_PLAN_TEXT)
    if item_type == "mcpToolCall":
        server = obj.get("server")
        tool = obj.get("tool")
        status = obj.get("status")
        return f"mcp[{server}.{tool}]: {status}", cap(json.dumps(obj, ensure_ascii=False), MAX_GENERIC)
    # Generic fallback for less-common types (webSearch, subAgentActivity,
    # collabAgentToolCall, imageView, plan-adjacent, contextCompaction, ...).
    dumped = json.dumps(obj, ensure_ascii=False)
    return f"{item_type}: " + cap(dumped, 150), cap(dumped, MAX_GENERIC)


def summarize_item_legacy(item_type, obj):
    """Best-effort summary for legacy (jsonl-only) items where field casing
    is inconsistent (snake_case / PascalCase mixed with camelCase)."""
    def g(*names):
        for n in names:
            if n in obj:
                return obj[n]
        return None

    if item_type == "userMessage":
        text = text_from_content(g("content"))
        return "user: " + cap(text, 200), cap(text, MAX_TEXT)
    if item_type == "agentMessage":
        text = g("text") or ""
        return "agent: " + cap(text, 200), cap(text, MAX_TEXT)
    if item_type == "reasoning":
        summary = g("summary", "summary_text") or []
        joined = " / ".join(summary) if isinstance(summary, list) else str(summary)
        return "reasoning: " + cap(joined, 200), cap(joined, MAX_TEXT)
    if item_type == "commandExecution":
        command = g("command") or ""
        status = g("status")
        return f"cmd[{status}]: " + cap(command, 200), cap(json.dumps(obj, ensure_ascii=False), MAX_GENERIC)
    if item_type == "fileChange":
        # Legacy jsonl shape differs from the sqlite-normalized one: `changes`
        # is a dict of {path: {type, content|unified_diff, ...}}, not a list.
        changes = g("changes") or {}
        if isinstance(changes, dict):
            paths = [f"{v.get('type')}:{p}" for p, v in changes.items()]
        else:
            paths = [f"{(c.get('kind') or {}).get('type')}:{c.get('path')}" for c in changes]
        return "filechange: " + cap(", ".join(paths), 300), cap(json.dumps(changes, ensure_ascii=False), MAX_GENERIC)
    if item_type == "plan":
        text = g("text") or ""
        return "plan update (" + str(len(text)) + " chars)", cap(text, MAX_PLAN_TEXT)
    dumped = json.dumps(obj, ensure_ascii=False)
    return f"{item_type}: " + cap(dumped, 150), cap(dumped, MAX_GENERIC)


def build_timeline(items, fidelity, max_per_type):
    counts = {}
    for it in items:
        counts[it["item_type"]] = counts.get(it["item_type"], 0) + 1

    # Cap noisy/high-volume types to the most recent N; keep narrative types
    # (user/agent messages, plans) closer to complete since they carry the
    # actual story a takeover needs.
    generous_types = {"userMessage", "agentMessage", "plan"}
    kept_counts = {}
    timeline = []
    files_touched = {}
    for it in reversed(items):  # walk newest-first so the cap keeps the tail
        t = it["item_type"]
        limit = None if max_per_type <= 0 else (max_per_type * 3 if t in generous_types else max_per_type)
        kept_counts[t] = kept_counts.get(t, 0) + 1
        if limit is not None and kept_counts[t] > limit:
            continue
        summarize = summarize_item_sqlite if fidelity == "sqlite" else summarize_item_legacy
        summary, detail = summarize(t, it["obj"])
        timeline.append(
            {
                "ordinal": it["ordinal"],
                "item_type": t,
                "created_at_ms": it.get("created_at_ms"),
                "summary": summary,
                "detail": detail,
            }
        )
        if t == "fileChange":
            changes = it["obj"].get("changes")
            if isinstance(changes, dict):
                # legacy jsonl shape: {path: {type, ...}}
                pairs = [(p, (v or {}).get("type")) for p, v in changes.items()]
            else:
                # sqlite-normalized shape: [{path, kind: {type}, diff}]
                pairs = [(c.get("path"), (c.get("kind") or {}).get("type")) for c in (changes or [])]
            for p, kind in pairs:
                if not p:
                    continue
                entry = files_touched.setdefault(p, {"path": p, "kinds": [], "last_ordinal": it["ordinal"]})
                if kind and kind not in entry["kinds"]:
                    entry["kinds"].append(kind)

    timeline.reverse()  # restore chronological order
    return timeline, counts, sorted(files_touched.values(), key=lambda e: e["last_ordinal"])


def git_env_check(cwd, recorded_branch, recorded_sha):
    result = {
        "cwd_exists": bool(cwd) and os.path.isdir(cwd),
        "is_git_repo": None,
        "current_branch": None,
        "current_head_sha": None,
        "branch_matches_recorded": None,
        "sha_matches_recorded": None,
        "worktree_dirty": None,
        "recorded_branch": recorded_branch,
        "recorded_sha": recorded_sha,
    }
    if not result["cwd_exists"]:
        return result
    try:
        inside = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        result["is_git_repo"] = inside.returncode == 0 and inside.stdout.strip() == "true"
        if not result["is_git_repo"]:
            return result
        br = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        sha = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        result["current_branch"] = br.stdout.strip() if br.returncode == 0 else None
        result["current_head_sha"] = sha.stdout.strip() if sha.returncode == 0 else None
        result["worktree_dirty"] = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
        if recorded_branch:
            result["branch_matches_recorded"] = result["current_branch"] == recorded_branch
        if recorded_sha:
            result["sha_matches_recorded"] = result["current_head_sha"] == recorded_sha
    except (subprocess.SubprocessError, OSError) as e:
        result["error"] = str(e)
    return result


def ms_to_iso(ms):
    if not ms:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))
    except (TypeError, ValueError, OSError):
        return None


def s_to_iso(s):
    if not s:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s))
    except (TypeError, ValueError, OSError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("thread", help="thread id (UUID) or codex://threads/<uuid> URI")
    ap.add_argument("--codex-home", default=None, help="override $CODEX_HOME (default: ~/.codex)")
    ap.add_argument("--max-items-per-type", type=int, default=DEFAULT_MAX_ITEMS_PER_TYPE,
                     help="cap noisy item types (commandExecution/fileChange/reasoning/...) to the N most recent; narrative types get 3x this. 0 = unlimited")
    ap.add_argument("--full", action="store_true", help="alias for --max-items-per-type 0")
    ap.add_argument("--raw-ordinal", type=int, default=None,
                     help="print the full, uncapped raw item_json at this rollout_ordinal and exit")
    args = ap.parse_args()

    if args.full:
        args.max_items_per_type = 0

    thread_id = extract_thread_id(args.thread)
    if not thread_id:
        print(json.dumps({"error": f"could not find a UUID in input: {args.thread!r}"}, ensure_ascii=False))
        sys.exit(2)

    home = codex_home(args.codex_home)
    if not os.path.isdir(home):
        print(json.dumps({"error": f"CODEX_HOME not found: {home}"}, ensure_ascii=False))
        sys.exit(2)

    meta_row = load_thread_meta(home, thread_id)
    items = load_paginated_items(home, thread_id)
    turns = load_turns(home, thread_id)
    fidelity = "sqlite"
    legacy_meta = {}

    if not items:
        recorded_path = meta_row.get("rollout_path") if meta_row else None
        rollout_path = find_rollout_path(home, thread_id, recorded_path)
        if rollout_path:
            items, legacy_meta = load_legacy_items(rollout_path)
            fidelity = "best_effort_legacy"

    if not meta_row and not items:
        print(json.dumps({
            "error": "thread not found in state_5.sqlite `threads`, thread_history_1.sqlite "
                     "`thread_items`, or via filename glob under sessions/ and archived_sessions/.",
            "thread_id": thread_id,
            "codex_home": home,
            "hint": "double-check the id, or run `codex resume` (no args) to browse the picker.",
        }, ensure_ascii=False))
        sys.exit(1)

    if args.raw_ordinal is not None:
        match = next((it for it in items if it["ordinal"] == args.raw_ordinal), None)
        if not match:
            print(json.dumps({"error": f"no item at ordinal {args.raw_ordinal}"}, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps(match, ensure_ascii=False, indent=2))
        return

    timeline, counts, files_touched = build_timeline(items, fidelity, args.max_items_per_type)

    cwd = (meta_row.get("cwd") if meta_row else None) or legacy_meta.get("cwd")
    recorded_branch = meta_row.get("git_branch") if meta_row else None
    recorded_sha = meta_row.get("git_sha") if meta_row else None
    env_check = git_env_check(cwd, recorded_branch, recorded_sha)

    plans = [t for t in timeline if t["item_type"] == "plan"]
    last_plan = plans[-1] if plans else None
    last_turn = turns[-1] if turns else None

    out = {
        "thread_id": thread_id,
        "fidelity": fidelity,  # "sqlite" (full fidelity) | "best_effort_legacy"
        "codex_home": home,
        "meta": {
            "cwd": cwd,
            "title": meta_row.get("title") if meta_row else None,
            "name": meta_row.get("name") if meta_row else None,
            "first_user_message": cap(meta_row.get("first_user_message"), MAX_TEXT) if meta_row else None,
            "git_branch": recorded_branch,
            "git_sha": recorded_sha,
            "git_origin_url": meta_row.get("git_origin_url") if meta_row else None,
            "model": meta_row.get("model") if meta_row else None,
            "cli_version": meta_row.get("cli_version") if meta_row else legacy_meta.get("cli_version"),
            "source": meta_row.get("source") if meta_row else legacy_meta.get("source"),
            "originator": legacy_meta.get("originator"),
            "created_at_iso": s_to_iso(meta_row.get("created_at")) if meta_row else None,
            "updated_at_iso": s_to_iso(meta_row.get("updated_at")) if meta_row else None,
            "archived": bool(meta_row.get("archived")) if meta_row else None,
            "history_mode": meta_row.get("history_mode") if meta_row else None,
            "rollout_path": meta_row.get("rollout_path") if meta_row else None,
        },
        "item_counts": counts,
        "turns_total": len(turns),
        "last_turn": last_turn,
        "failed_or_error_turns": [t for t in turns if t["status"] not in ("completed", None)],
        "last_plan": last_plan,
        "files_touched": files_touched,
        "timeline": timeline,
        "env_check": env_check,
        "notes": [
            "This is a read of undocumented local Codex internals; treat schema drift on Codex "
            "version upgrades as expected, not as a bug to silently paper over.",
            "fidelity=best_effort_legacy means this thread was reconstructed from the raw jsonl "
            "rollout log rather than the paginated sqlite index; some metadata (e.g. git_branch/"
            "git_sha) may be unavailable and is reported as null rather than guessed.",
            f"Noisy item types were capped to the most recent {args.max_items_per_type or 'unlimited'} "
            "per type (narrative types get 3x). Rerun with --full or --raw-ordinal <n> for more.",
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
