#!/usr/bin/env python3
"""Quiet observer for a host-managed background task. Never starts model work.

Print one terminal receipt, then let the host's native completion notice wake the
original conversation. A timeout is an observer failure, not a review result.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def observe(engine, job_id, state_dir=None, interval=5, timeout=3600, url=None, dsh_home=None):
    root = Path(__file__).resolve().parent
    env = os.environ.copy()
    if engine == "opus":
        command = [sys.executable, str(root / "opus.py"), "monitor", job_id]
        if state_dir:
            command.extend(["--state-dir", str(Path(state_dir).resolve())])
            env["OPUS_REVIEW_STATE_DIR"] = str(Path(state_dir).resolve())
    else:
        command = [sys.executable, str(root.parents[1] / "dsh-review/scripts/dsh.py"), "monitor", job_id]
        for key, value in [("--state-dir", state_dir), ("--url", url), ("--dsh-home", dsh_home)]:
            if value:
                command.extend([key, str(value)])
    deadline = time.monotonic() + timeout
    while True:
        try:
            child = subprocess.run(command, env=env, text=True, capture_output=True, timeout=45)
            value = json.loads(child.stdout)
            if child.returncode or value.get("monitorFault") or "error" in value and "status" not in value:
                raise RuntimeError(value.get("error") or "Monitor query failed")
        except (ValueError, OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return {"jobId": job_id, "watchStatus": "observer_failed", "fallbackRequired": True,
                    "notify": False, "stopMonitor": False, "error": str(exc)}, 2
        # A second path may already hold the notification lease. It is still a
        # terminal review; the host must claim before publishing any result.
        if value.get("status") in {"completed", "failed", "cancelled", "needs_input"} and not value.get("pendingNotification"):
            compact = {k: v for k, v in value.items() if k != "result"}
            return {**compact, "watchStatus": "terminal", "fallbackRequired": False}, 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"jobId": job_id, "watchStatus": "observer_timeout", "fallbackRequired": True,
                    "notify": False, "stopMonitor": False}, 4
        time.sleep(min(interval, remaining))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--engine", choices=["opus", "dsh"], required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--state-dir")
    p.add_argument("--url")
    p.add_argument("--dsh-home")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--timeout", type=float, default=3600)
    args = vars(p.parse_args())
    if args["interval"] <= 0 or args["timeout"] <= 0:
        p.error("interval and timeout must be positive")
    result, code = observe(**args)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
