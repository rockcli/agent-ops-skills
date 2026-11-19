---
name: codex-send-continue
description: "Continue an existing Codex task with a follow-up message. Prefer the current client's native task tools; use a CLI fallback only when those tools are unavailable."
---

# Continue an existing Codex task

Identify the exact existing task with the current client's list/read tools. Preserve the user's target, message, and scope; if several tasks match, ask for the missing identifier rather than guessing.

When available, use the native `send_message_to_thread` capability. A status-only request uses read/wait capabilities and does not send a message. Do not create a new user task or repeatedly send `continue` as a polling mechanism.

Distinguish a message being accepted from the task finishing. Inspect native status/turn evidence when completion is part of the request. If delivery is uncertain, check for the existing message before retrying to avoid duplicate work.

If native delivery tools are unavailable, read [CLI fallback](references/cli-fallback.md), verify the installed CLI and the target's cwd, and reconcile writer ownership before resuming. A request to continue does not authorize closing the user's desktop application, deleting a writer lock, or interrupting unrelated tasks. Explain the impact and obtain authorization if releasing a confirmed lock requires such an action.
