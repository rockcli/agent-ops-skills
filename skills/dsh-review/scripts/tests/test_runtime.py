import contextlib
import hashlib
import hmac
import http.server
import importlib.util
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "dsh.py"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dsh-wire.json"
spec = importlib.util.spec_from_file_location("dsh_jobs", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _job():
    return {"jobId": "a" * 32, "requestId": "owned", "status": "queued", "cursor": 0}


def _event(seq, etype, data=None):
    return {"seq": seq, "type": etype, "data": data or {}}


def _message(seq, text):
    return _event(
        seq,
        "assistant/message",
        {
            "message": {
                "content": [{"type": "text", "text": text}],
                "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-flash"},
            }
        },
    )


def _anchor(seq):
    return _event(seq, "user/message", {"source": {"rpcId": "owned"}})


def _end(seq, kind="completed", turn=2):
    return _event(seq, "turn/end", {"turn": turn, "reason": {"kind": kind}})


class FakeDshServer:
    """In-process loopback HTTP server speaking the DSH Remote API with cookie HMAC validation."""

    def __init__(self, secret: bytes):
        self.secret = secret
        self.calls: list[tuple[str, dict]] = []
        self.sessions: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.flash_available = True
        self.default_provider = "deepseek-official"
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                authority = self.headers.get("Host", "")
                expected_name = "dsh-auth-" + m._b64url_encode(hashlib.sha256(authority.encode()).digest())
                cookie_header = self.headers.get("Cookie", "")
                authed = False
                for part in cookie_header.split(";"):
                    part = part.strip()
                    if part.startswith(expected_name + "="):
                        val = part.split("=", 1)[1]
                        segs = val.split(".")
                        if len(segs) == 3 and segs[0] == "v1":
                            sig = m._b64url_encode(hmac.new(parent.secret, segs[1].encode("ascii"), hashlib.sha256).digest())
                            if hmac.compare_digest(sig, segs[2]):
                                authed = True
                if not authed:
                    self.send_response(401)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                endpoint = self.path.removeprefix("/api/")
                args = (payload.get("payload") or {}).get("args") or {}
                parent.calls.append((endpoint, args))
                value = parent._handle(endpoint, args)
                body = json.dumps({"result": {"ok": True, "value": value}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handle(self, endpoint: str, args: dict):
        if endpoint == "session/modelCatalog":
            models = [{"id": "deepseek-flash"}] if self.flash_available else []
            return {
                "default": {"provider": self.default_provider},
                "groups": [{"id": "deepseek-official", "models": models}],
            }
        if endpoint == "session/selectModel":
            req = args["request"]
            return {
                "selected": {
                    "provider": req["provider"],
                    "model": req["model"],
                    "reasoningEffort": req["reasoningEffort"],
                }
            }
        if endpoint == "session/create":
            sid = f"sess-{len(self.sessions) + 1}"
            self.sessions[sid] = {
                "sessionId": sid,
                "cwd": args["request"].get("cwd", ""),
                "running": True,
                "projections": {"asOfSeq": 0, "values": {"title": "", "inbox": {"next-turn": [], "next-step": []}}},
            }
            self.events[sid] = []
            return {"sessionId": sid}
        if endpoint == "session/rename":
            sid = args["request"]["sessionId"]
            self.sessions[sid]["projections"]["values"]["title"] = args["request"]["title"]
            return {}
        if endpoint == "session/prompt":
            req = args["request"]
            sid = req["sessionId"]
            evs = self.events[sid]
            evs.extend(
                [
                    _event(1, "turn/start", {"turn": 1}),
                    _event(2, "user/message", {"source": {"rpcId": req["requestId"]}}),
                    _message(3, "DSH unified review finding"),
                    _end(4, "completed", 1),
                ]
            )
            self.sessions[sid]["projections"]["asOfSeq"] = 4
            self.sessions[sid]["running"] = False
            return {"accepted": True}
        if endpoint == "session/list":
            return {"items": list(self.sessions.values())}
        if endpoint == "session/page":
            req = args["request"]
            sid = req["address"]["sessionId"]
            through = req["throughSeq"]
            rows = [{"type": "event", "event": e} for e in self.events.get(sid, []) if e["seq"] <= through]
            return {"records": rows, "hasMore": False}
        if endpoint == "session/cancel":
            sid = args["request"]["sessionId"]
            self.sessions[sid]["running"] = False
            return {}
        return {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DshRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.dsh_home = self.root / "dsh-home"
        self.dsh_home.mkdir()
        self.secret = b"0123456789abcdef0123456789abcdef"
        (self.dsh_home / ".credentials.yaml").write_text(
            f"client-connection/browser-session:\n  secret: {m._b64url_encode(self.secret)}\n",
            encoding="utf-8",
        )
        self.old_state = m.STATE
        m.STATE = self.state

    def tearDown(self):
        m.STATE = self.old_state
        self.tmp.cleanup()

    def _run(self, *args, check=True):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout), proc.returncode

    def _seed_terminal(self, job_id="a" * 32, *, legacy=False, status="completed"):
        directory = (self.state / job_id) if legacy else (self.state / "jobs" / job_id)
        directory.mkdir(parents=True, exist_ok=True)
        job = {
            "jobId": job_id,
            "kind": "review",
            "status": status,
            "createdAt": m.now(),
            "repo": str(self.root),
            "ownerHost": "claude",
            "ownerThreadId": "conv-1",
            "owner": {"host": "claude", "id": "conv-1"},
            "sessionId": "sess-legacy",
            "reply": "isolated result",
            "model": "deepseek-flash",
            "target": {"repo": str(self.root), "scope": "working-tree", "files": {"a.py": "deadbeef"}},
            "cursor": 10,
            "notifiedAt": None,
        }
        (directory / "job.json").write_text(json.dumps(job), encoding="utf-8")
        return directory / "job.json"

    # ---- Ported 15 reducer & stopOwned tests from dsh-review.test.mjs ----

    def test_captured_real_dsh_wire_yields_review_text_and_model(self):
        events = json.loads(FIXTURE.read_text(encoding="utf-8"))
        request_id = next(e["data"]["source"]["rpcId"] for e in events if (e.get("data") or {}).get("source", {}).get("rpcId"))
        value = m.reduce_events({**_job(), "requestId": request_id}, events)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["model"], "deepseek-flash")
        self.assertIn("value -= item", value["reply"])
        self.assertIn("totals.py", value["reply"])

    def test_old_replies_and_old_turn_ends_cannot_satisfy_new_request(self):
        value = m.reduce_events(_job(), [_message(1, "old review"), _end(2)])
        self.assertEqual(value["status"], "queued")
        self.assertFalse(m.presentation(value)["notify"])
        self.assertFalse(m.presentation(value)["stopMonitor"])

    def test_partial_reply_stays_silent_until_owned_turn_ends(self):
        value = m.reduce_events(_job(), [_event(1, "turn/start", {"turn": 2}), _anchor(2), _message(3, "partial")])
        self.assertEqual(value["status"], "running")
        self.assertFalse(m.presentation(value)["notify"])
        value = json.loads(json.dumps(value))
        m.reduce_events(value, [_message(3, "partial"), _message(4, "[P1] subtracts"), _end(5)])
        self.assertEqual(value["reply"], "[P1] subtracts")
        self.assertEqual(value["status"], "completed")
        self.assertTrue(m.presentation(value)["notify"])
        value["notifiedAt"] = "ack"
        self.assertFalse(m.presentation(value)["notify"])
        self.assertTrue(m.presentation(value)["stopMonitor"])

    def test_provider_error_cannot_become_successful_from_partial_text(self):
        value = m.reduce_events(_job(), [_anchor(1), _message(2, "partial"), _end(3, "error")])
        self.assertEqual(value["status"], "failed")
        self.assertTrue(m.presentation(value)["stopMonitor"])

    def test_empty_completed_reply_is_failure(self):
        self.assertEqual(m.reduce_events(_job(), [_anchor(1), _end(2)])["status"], "failed")

    def test_cancellation_and_interrupted_retain_cancelled(self):
        for reason in ["aborted", "interrupted"]:
            self.assertEqual(m.reduce_events(_job(), [_anchor(1), _message(2, "partial"), _end(3, reason)])["status"], "cancelled")

    def test_open_user_question_stops_monitor_with_needs_input(self):
        value = m.reduce_events(
            _job(),
            [_anchor(1), _event(2, "tool/call", {"callId": "q", "name": "ask_user_question", "arguments": {"questions": ["which?"]}})],
        )
        self.assertEqual(value["status"], "needs_input")
        self.assertTrue(m.presentation(value)["notify"])

    def test_already_answered_question_is_not_blocking(self):
        value = m.reduce_events(
            _job(),
            [
                _anchor(1),
                _event(2, "tool/call", {"callId": "q", "name": "ask_user_question", "arguments": {}}),
                _event(3, "tool/result", {"message": {"source": {"callId": "q"}}}),
                _message(4, "result"),
                _end(5),
            ],
        )
        self.assertEqual(value["status"], "completed")

    def test_needs_input_can_refresh_after_real_answer(self):
        value = m.reduce_events(
            _job(),
            [
                _event(1, "turn/start", {"turn": 2}),
                _anchor(2),
                _event(3, "tool/call", {"callId": "q", "name": "ask_user_question", "arguments": {}}),
            ],
        )
        value["notifiedAt"] = "question already reported"
        m.reduce_events(value, [])
        self.assertEqual(value["status"], "needs_input")
        self.assertEqual(value["notifiedAt"], "question already reported")
        m.reduce_events(
            value,
            [
                _event(4, "tool/result", {"message": {"source": {"callId": "q"}}}),
                _message(5, "review result"),
                _end(6),
            ],
        )
        self.assertEqual(value["status"], "completed")
        self.assertNotIn("notifiedAt", value)
        self.assertTrue(m.presentation(value)["notify"])

    def test_unrelated_additional_input_fails_job(self):
        value = m.reduce_events(_job(), [_anchor(1), _event(2, "user/message", {"source": {"rpcId": "other"}}), _message(3, "new")])
        self.assertEqual(value["status"], "failed")
        self.assertNotIn("reply", value)

    def test_dsh_injected_messages_are_ignored(self):
        injected = [
            _event(i + 3, "user/message", {"source": {"kind": kind}, "content": [{"type": "text", "text": "ctx"}]})
            for i, kind in enumerate(["agent-instructions", "runtime-context", "skill-catalog", "time-context"])
        ]
        value = m.reduce_events(_job(), [_event(1, "turn/start", {"turn": 2}), _anchor(2), *injected, _message(8, "review"), _end(9)])
        self.assertEqual(value["status"], "completed")

    def test_turn_identity_survives_checkpoint_and_ignores_unrelated_turn_end(self):
        value = m.reduce_events(_job(), [_event(1, "turn/start", {"turn": 2})])
        m.reduce_events(value, [_anchor(2), _message(3, "review"), _end(4, "completed", 1)])
        self.assertEqual(value["status"], "running")
        m.reduce_events(value, [_end(5)])
        self.assertEqual(value["status"], "completed")

    def test_stop_owned_removes_only_owned_queue_item_and_cancels(self):
        calls = []
        own = {"id": "owned-item", "source": {"rpcId": "owned"}, "content": []}
        other = {"id": "other-item", "source": {"rpcId": "other"}, "content": []}
        states = [
            {"running": True, "inbox": {"next-turn": [own, other]}},
            {"running": False, "inbox": {"next-turn": [other]}},
        ]

        class Client:
            def rpc(self, name, args):
                calls.append((name, args))

        m.stop_owned(Client(), {"sessionId": "isolated", "requestId": "owned"}, inspect=lambda _c, _s: states.pop(0))
        self.assertEqual(calls[0][0], "session/updateQueue")
        self.assertEqual(calls[0][1]["request"]["itemId"], "owned-item")
        self.assertEqual(calls[1][0], "session/cancel")
        self.assertEqual(len(calls), 2)

    def test_stop_owned_handles_claim_race_during_queue_removal(self):
        own = {"id": "owned-item", "source": {"rpcId": "owned"}, "content": []}
        states = [
            {"running": True, "inbox": {"next-turn": [own]}},
            {"running": True, "inbox": {}},
            {"running": False, "inbox": {}},
        ]
        calls = []

        class Client:
            def rpc(self, name, args):
                calls.append(name)
                if name == "session/updateQueue":
                    raise RuntimeError("already claimed")

        m.stop_owned(Client(), {"sessionId": "isolated", "requestId": "owned"}, inspect=lambda _c, _s: states.pop(0))
        self.assertEqual(calls, ["session/updateQueue", "session/cancel"])

    # ---- Unified Python Contract & Integration Tests ----

    def test_start_task_watch_result_and_setup_against_fake_dsh_server(self):
        server = FakeDshServer(self.secret)
        try:
            # 1) setup checks credentials, cookie HMAC, and flash availability
            setup_res, _ = self._run("setup", "--url", server.url, "--dsh-home", str(self.dsh_home))
            self.assertTrue(setup_res["ready"])
            self.assertEqual(setup_res["model"], "deepseek-flash")
            self.assertEqual(setup_res["effort"], "max")

            # 2) start freezes repo, strips target.files from output, enforces flash+max
            repo = self.root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "init"],
                check=True,
            )
            (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
            prompt_file = self.root / "prompt.txt"
            prompt_file.write_text("Review the change.", encoding="utf-8")

            started, _ = self._run(
                "start",
                "--repo",
                str(repo),
                "--owner-host",
                "claude",
                "--owner-thread",
                "conv-1",
                "--prompt-file",
                str(prompt_file),
                "--state-dir",
                str(self.state),
                "--url",
                server.url,
                "--dsh-home",
                str(self.dsh_home),
            )
            self.assertEqual(started["kind"], "review")
            self.assertEqual(started["status"], "queued")
            self.assertNotIn("files", started["target"])
            self.assertEqual(started["target"]["fileCount"], 1)

            # 3) watch completes silently and returns compact single-line terminal JSON
            watched, code = self._run(
                "--state-dir",
                str(self.state),
                "watch",
                started["jobId"],
                "--interval",
                "0.05",
                "--timeout",
                "5",
            )
            self.assertEqual(code, 0)
            self.assertEqual(watched["watchStatus"], "terminal")
            self.assertEqual(watched["status"], "completed")
            self.assertNotIn("result", watched)

            # 4) result returns unified result.response and compact target
            res, _ = self._run("result", started["jobId"], "--state-dir", str(self.state))
            self.assertEqual(res["result"]["response"], "DSH unified review finding")
            self.assertEqual(res["result"]["model"], "deepseek-flash")
            self.assertEqual(res["result"]["modelSource"], "dsh_event_log")
            self.assertNotIn("files", res["target"])

            # 5) task works read-only without needing git diff
            task_dir = self.root / "plain-dir"
            task_dir.mkdir()
            task_started, _ = self._run(
                "task",
                "--repo",
                str(task_dir),
                "--owner-host",
                "claude",
                "--owner-thread",
                "conv-1",
                "--prompt-file",
                str(prompt_file),
                "--state-dir",
                str(self.state),
                "--url",
                server.url,
                "--dsh-home",
                str(self.dsh_home),
            )
            self.assertEqual(task_started["kind"], "task")
            self.assertFalse(task_started["write"])
            self.assertIsNone(task_started["target"])
            task_res, _ = self._run("result", task_started["jobId"], "--state-dir", str(self.state))
            self.assertEqual(task_res["status"], "completed")
            self.assertEqual(task_res["result"]["response"], "DSH unified review finding")

            # 6) status without jobId lists both jobs & supports --repo filter
            listed, _ = self._run("status", "--state-dir", str(self.state))
            self.assertEqual(len(listed["jobs"]), 2)
            filtered, _ = self._run("status", "--repo", str(task_dir), "--state-dir", str(self.state))
            self.assertEqual([j["jobId"] for j in filtered["jobs"]], [task_started["jobId"]])
        finally:
            server.close()

    def test_bind_native_and_bind_monitor_unified_and_legacy_syntax(self):
        job_id = "b" * 32
        self._seed_terminal(job_id)

        # Unified positional bind-native
        bound, _ = self._run("--state-dir", str(self.state), "bind-native", job_id, "claude", "task-1")
        self.assertEqual(bound["nativeTask"], {"host": "claude", "id": "task-1", "owner": "conv-1"})

        # Legacy named bind-native with --replace + --state-dir after subcommand
        replaced, _ = self._run(
            "bind-native", job_id, "--host", "claude", "--monitor-id", "task-2", "--replace", "task-1", "--state-dir", str(self.state)
        )
        self.assertEqual(replaced["nativeTask"]["id"], "task-2")

        # Wrong host rejected
        _, code = self._run("bind-native", job_id, "codex", "task-3", "--replace", "task-2", "--state-dir", str(self.state), check=False)
        self.assertNotEqual(code, 0)

        # Unified positional bind-monitor
        mon, _ = self._run("bind-monitor", job_id, "sched-1", "--state-dir", str(self.state))
        self.assertEqual(mon["status"], "completed")
        st, _ = self._run("status", job_id, "--state-dir", str(self.state))
        self.assertEqual(st["monitorId"], "sched-1")

    def test_legacy_state_layout_and_concurrent_claim_ack(self):
        job_id = "c" * 32
        self._seed_terminal(job_id, legacy=True)

        # Target files stripped even for legacy job.json
        st, _ = self._run("status", job_id, "--state-dir", str(self.state))
        self.assertNotIn("files", st["target"])
        self.assertEqual(st["target"]["fileCount"], 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: m.claim_notification(job_id, f"native:{i}"), range(12)))
        winners = [c for c in claims if c["notify"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["result"]["response"], "isolated result")
        with self.assertRaisesRegex(ValueError, "claim token"):
            m.monitor(job_id, ack=True, claim_token="wrong")
        m.monitor(job_id, ack=True, claim_token=winners[0]["claimToken"])
        replay = m.claim_notification(job_id, "native:replay")
        self.assertFalse(replay["notify"])
        self.assertTrue(replay["stopMonitor"])

    def test_watch_missing_job_and_timeout_and_quiet_pending_claim(self):
        # Missing job fails fast with exit 2
        val, code = self._run("watch", "f" * 32, "--state-dir", str(self.state), "--interval", "0.05", "--timeout", "1", check=False)
        self.assertEqual(code, 2)
        self.assertEqual(val["watchStatus"], "observer_failed")
        self.assertTrue(val["fallbackRequired"])

        # Active claim holds watch silently until lease expires
        job_id = "d" * 32
        path = self._seed_terminal(job_id)
        self._run("claim", job_id, "--source", "native:holder", "--state-dir", str(self.state))
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "watch", job_id, "--state-dir", str(self.state), "--interval", "0.05", "--timeout", "5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        self.assertIsNone(proc.poll())
        self.assertEqual(select.select([proc.stdout, proc.stderr], [], [], 0)[0], [])
        job = json.loads(path.read_text(encoding="utf-8"))
        job["notificationClaim"]["expiresAt"] = 0
        path.write_text(json.dumps(job), encoding="utf-8")
        out, err = proc.communicate(timeout=5)
        self.assertEqual(err, "")
        self.assertEqual(proc.returncode, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["watchStatus"], "terminal")
        self.assertTrue(parsed["notify"])

    def test_needs_input_blocked_survives_empty_refresh_and_authority_normalized(self):
        # P1-1 regression: blocked without questions must stay needs_input on empty refresh
        value = m.reduce_events(_job(), [_anchor(1), _end(2, "blocked", turn=1)])
        self.assertEqual(value["status"], "needs_input")
        self.assertTrue(m.presentation(value)["notify"])
        m.reduce_events(value, [])
        self.assertEqual(value["status"], "needs_input")
        self.assertTrue(m.presentation(value)["notify"])

        # P2-1 regression: uppercase host & default port normalized for cookie authority
        client = m.DshClient("http://LOCALHOST:3080", self.dsh_home)
        self.assertEqual(client.authority, "localhost:3080")
        client80 = m.DshClient("http://LOCALHOST:80", self.dsh_home)
        self.assertEqual(client80.authority, "localhost")

        # help / empty args return usage JSON with exit 0
        usage, code = self._run("help")
        self.assertEqual(code, 0)
        self.assertIn("usage", usage)


if __name__ == "__main__":
    unittest.main(verbosity=2)
