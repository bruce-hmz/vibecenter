import importlib.util
import io
import json
import hmac
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
RELAY_PATH = REPO_ROOT / "relay.py"
INSTALLER_PATH = REPO_ROOT / "install-hook.sh"


def load_relay_module():
    spec = importlib.util.spec_from_file_location("relay_module", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.relay = load_relay_module()

    def write_token(self, tmpdir: str, token_hex: str = "11" * 32) -> Path:
        token_path = Path(tmpdir) / "ipc-token"
        token_path.write_text(token_hex, encoding="utf-8")
        return token_path

    def sign_response(self, payload: dict[str, object], token_hex: str = "11" * 32) -> dict[str, object]:
        unsigned_payload = dict(payload)
        unsigned_payload["auth_nonce"] = "response-nonce"
        signature = hmac.new(
            bytes.fromhex(token_hex),
            self.relay.canonical_json_bytes(unsigned_payload),
            self.relay.hashlib.sha256,
        ).hexdigest()
        unsigned_payload["auth_signature"] = signature
        return unsigned_payload

    def test_normalize_question_keeps_multiselect(self):
        question = self.relay.normalize_question(
            {
                "id": "owners",
                "header": "owners",
                "question": "Who owns this?",
                "multiSelect": True,
                "options": [
                    {"label": "Alice", "description": "Backend"},
                    {"label": "Bob", "description": "Frontend"},
                ],
            },
            0,
        )

        self.assertTrue(question["multiSelect"])
        self.assertEqual(question["options"][0]["description"], "Backend")

    def test_build_ask_payload_keeps_full_questions_and_legacy_options(self):
        normalized = [
            self.relay.normalize_question(
                {
                    "header": "priority",
                    "question": "Which priority?",
                    "options": [{"label": "High"}, {"label": "Low"}],
                },
                0,
            ),
            self.relay.normalize_question(
                {
                    "header": "owners",
                    "question": "Who owns this?",
                    "multiSelect": True,
                    "options": [{"label": "Alice"}, {"label": "Bob"}],
                },
                1,
            ),
        ]

        payload = self.relay.build_ask_payload(
            event={"session_id": "sess-1", "_source": "claude"},
            request_id="req-1",
            agent_name="Claude Code",
            short_cwd="repo",
            normalized_questions=normalized,
        )

        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["timeout_seconds"], 300)
        self.assertEqual(payload["questions"][1]["question"], "Who owns this?")
        self.assertTrue(payload["questions"][1]["multiSelect"])
        self.assertEqual(payload["options"], ["High", "Low"])

    def test_request_approval_includes_timeout_and_full_cwd(self):
        relay = self.relay
        captured = {}
        real_send = relay.send

        def fake_send(payload, wait_response=False):
            self.assertTrue(wait_response)
            captured.update(payload)
            return ({"request_id": payload["request_id"], "action": "deny"}, None)

        relay.send = fake_send
        try:
            action, error = relay.request_approval(
                {
                    "session_id": "sess-1",
                    "cwd": "/tmp/example-project",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status"},
                },
                "Claude Code",
                "PreToolUse",
            )
        finally:
            relay.send = real_send

        self.assertIsNone(error)
        self.assertEqual(action, "deny")
        self.assertEqual(captured["cwd"], "/tmp/example-project")
        self.assertEqual(captured["timeout_seconds"], 300)

    def test_push_session_omits_cwd_and_supports_allowed_event_kinds(self):
        relay = self.relay
        captured = {}
        real_send = relay.send

        def fake_send(payload, wait_response=False):
            captured.update(payload)
            return ({"ok": True}, None)

        relay.send = fake_send
        try:
            relay.push_session(
                {"session_id": "sess-1", "cwd": "/tmp/project", "_source": "claude"},
                "update",
                "Idle",
                detail="project",
                running=False,
                event_kind="completed",
            )
        finally:
            relay.send = real_send

        self.assertEqual(captured["session"], "update")
        self.assertEqual(captured["task"], "Idle")
        self.assertEqual(captured["detail"], "project")
        self.assertEqual(captured["event_kind"], "completed")
        self.assertFalse(captured["running"])
        self.assertNotIn("cwd", captured)

    def test_push_session_rejects_invalid_event_kind(self):
        with self.assertRaises(ValueError):
            self.relay.push_session(
                {"session_id": "sess-1"},
                "update",
                "Idle",
                event_kind="tool_failure",
            )

    def test_stop_events_use_completed_or_failed_event_kind_without_event_notice(self):
        relay = self.relay
        events = [
            {
                "hook_event_name": "Stop",
                "session_id": "sess-1",
                "cwd": "/tmp/project",
                "last_assistant_message": "Done with the task",
            },
            {
                "hook_event_name": "StopFailure",
                "session_id": "sess-2",
                "cwd": "/tmp/project",
                "error": "Command exploded",
            },
        ]

        for event, expected_kind, expected_running in [
            (events[0], "completed", False),
            (events[1], "failed", False),
        ]:
            payloads = []
            real_send = relay.send
            real_read_event = relay.read_event
            try:
                relay.send = lambda payload, wait_response=False: (payloads.append(payload) or ({"ok": True}, None))
                relay.read_event = lambda: event
                result = relay.main()
            finally:
                relay.send = real_send
                relay.read_event = real_read_event

            self.assertEqual(result, 0)
            self.assertFalse(any(payload.get("type") == "event_notice" for payload in payloads))
            session_payloads = [payload for payload in payloads if "session" in payload]
            self.assertEqual(len(session_payloads), 1)
            self.assertEqual(session_payloads[0]["event_kind"], expected_kind)
            self.assertEqual(session_payloads[0]["running"], expected_running)
            self.assertNotIn("cwd", session_payloads[0])

    def test_posttoolusefailure_marks_session_failed_without_event_notice(self):
        relay = self.relay
        payloads = []
        real_send = relay.send
        real_read_event = relay.read_event
        try:
            relay.send = lambda payload, wait_response=False: (payloads.append(payload) or ({"ok": True}, None))
            relay.read_event = lambda: {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "sess-1",
                "cwd": "/tmp/project",
                "tool_name": "Bash",
                "tool_input": {"command": "echo sensitive"},
                "error": "sensitive failure body",
            }
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event

        self.assertEqual(result, 0)
        self.assertFalse(any(payload.get("type") == "event_notice" for payload in payloads))
        session_payloads = [payload for payload in payloads if "session" in payload]
        self.assertEqual(len(session_payloads), 1)
        self.assertEqual(session_payloads[0]["event_kind"], "failed")
        self.assertEqual(session_payloads[0]["task"], "Failed: Bash")
        self.assertNotIn("cwd", session_payloads[0])

    def test_notification_waiting_event_kind_is_limited_to_supported_types(self):
        relay = self.relay
        cases = [
            ("permission_prompt", True),
            ("idle_prompt", True),
            ("elicitation_dialog", True),
            ("", True),
            ("auth_success", False),
            ("agent_needs_input", False),
        ]

        for notification_type, should_send_session in cases:
            payloads = []
            real_send = relay.send
            real_read_event = relay.read_event
            try:
                relay.send = lambda payload, wait_response=False: (payloads.append(payload) or ({"ok": True}, None))
                relay.read_event = lambda nt=notification_type: {
                    "hook_event_name": "Notification",
                    "session_id": "sess-1",
                    "cwd": "/tmp/project",
                    "notification_type": nt,
                    "message": "Waiting for input",
                }
                result = relay.main()
            finally:
                relay.send = real_send
                relay.read_event = real_read_event

            self.assertEqual(result, 0)
            session_payloads = [payload for payload in payloads if "session" in payload]
            if should_send_session:
                self.assertEqual(len(session_payloads), 1)
                self.assertEqual(session_payloads[0]["event_kind"], "waiting")
                self.assertNotIn("cwd", session_payloads[0])
            else:
                self.assertEqual(session_payloads, [])

    def test_parse_ask_answers_uses_question_text_and_joins_multiselect(self):
        normalized = [
            self.relay.normalize_question(
                {
                    "header": "priority",
                    "question": "Which priority?",
                    "options": [{"label": "High"}],
                },
                0,
            ),
            self.relay.normalize_question(
                {
                    "header": "owners",
                    "question": "Who owns this?",
                    "multiSelect": True,
                    "options": [{"label": "Alice"}, {"label": "Bob"}],
                },
                1,
            ),
        ]

        answers, error = self.relay.parse_ask_answers(
            {
                "request_id": "req-1",
                "answers": {
                    "Which priority?": "High",
                    "owners": ["Alice", "Bob"],
                },
            },
            "req-1",
            normalized,
        )

        self.assertIsNone(error)
        self.assertEqual(
            answers,
            {
                "Which priority?": "High",
                "Who owns this?": "Alice, Bob",
            },
        )

    def test_parse_ask_answers_accepts_question_ids_and_requires_request_id(self):
        normalized = [
            self.relay.normalize_question(
                {
                    "id": "priority-id",
                    "header": "priority",
                    "question": "Which priority?",
                    "options": [{"label": "High"}],
                },
                0,
            )
        ]

        answers, error = self.relay.parse_ask_answers(
            {
                "request_id": "req-1",
                "answers": {"priority-id": "High"},
            },
            "req-1",
            normalized,
        )
        self.assertIsNone(error)
        self.assertEqual(answers, {"Which priority?": "High"})

        answers, error = self.relay.parse_ask_answers(
            {"answers": {"priority-id": "High"}},
            "req-1",
            normalized,
        )
        self.assertIsNone(answers)
        self.assertIn("request_id mismatch", error)

        answers, error = self.relay.parse_ask_answers(
            {
                "request_id": "req-1",
                "action": "cancel",
                "reason": "request_timeout",
            },
            "req-1",
            normalized,
        )
        self.assertIsNone(answers)
        self.assertEqual(error, "cancelled:request_timeout")

    def test_generated_request_id_is_stable_and_input_sensitive(self):
        event = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        }

        first = self.relay.request_id_for(event, "PreToolUse", "Bash")
        second = self.relay.request_id_for(event, "PreToolUse", "Bash")
        changed = self.relay.request_id_for(
            {**event, "tool_input": {"command": "git diff"}},
            "PreToolUse",
            "Bash",
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_pretool_write_requests_approval_and_fails_closed(self):
        relay = self.relay
        real_send = relay.send
        buffer = io.StringIO()

        def fake_send(payload, wait_response=False):
            if not wait_response:
                return ({"ok": True}, None)
            self.assertEqual(payload["request_kind"], "approval")
            self.assertEqual(payload["tool_name"], "Bash")
            return (None, "notch unavailable")

        relay.send = fake_send
        try:
            with redirect_stdout(buffer):
                result = relay.handle_pretool_approval(
                    {
                        "session_id": "sess-1",
                        "tool_name": "Bash",
                        "tool_input": {"command": "git status"},
                    },
                    "Claude Code",
                )
        finally:
            relay.send = real_send

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("unavailable", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_pretool_skips_approval_in_bypass_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called in bypass mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-bypass",
                "permission_mode": "bypassPermissions",
                "tool_name": "Bash",
                "tool_input": {"command": "git push"},
                "cwd": "/tmp/project",
            }
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        self.assertEqual(result, 0)

    def test_pretool_skips_edit_approval_in_accept_edits_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called for edits in acceptEdits mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/project/app.py"},
                "cwd": "/tmp/project",
            }
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        self.assertEqual(result, 0)

    def test_pretool_still_approves_bash_in_accept_edits_mode(self):
        relay = self.relay
        payloads = []
        real_send = relay.send
        real_read_event = relay.read_event

        def fake_send(payload, wait_response=False):
            if not wait_response:
                payloads.append(payload)
                return ({"ok": True}, None)
            self.assertEqual(payload["request_kind"], "approval")
            return (None, "notch unavailable")

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /tmp/old"},
                "cwd": "/tmp/project",
            }
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        # Bash still requires approval even in acceptEdits mode
        self.assertTrue(any(p.get("request_kind") == "approval" for p in payloads))
        self.assertEqual(result, 0)

    def test_pretool_skips_approval_in_bypass_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_path = os.environ.get("VIBE_ISLAND_DEDUP_PATH")
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called in bypass mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-bypass",
                "permission_mode": "bypassPermissions",
                "tool_name": "Bash",
                "tool_input": {"command": "git push"},
                "cwd": "/tmp/project",
            }
            os.environ["VIBE_ISLAND_DEDUP_PATH"] = tempfile.mktemp()
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            if real_dedup_path is None:
                os.environ.pop("VIBE_ISLAND_DEDUP_PATH", None)
            else:
                os.environ["VIBE_ISLAND_DEDUP_PATH"] = real_dedup_path
        self.assertEqual(result, 0)

    def test_pretool_skips_edit_approval_in_accept_edits_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_path = os.environ.get("VIBE_ISLAND_DEDUP_PATH")
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called for edits in acceptEdits mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/project/app.py"},
                "cwd": "/tmp/project",
            }
            os.environ["VIBE_ISLAND_DEDUP_PATH"] = tempfile.mktemp()
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            if real_dedup_path is None:
                os.environ.pop("VIBE_ISLAND_DEDUP_PATH", None)
            else:
                os.environ["VIBE_ISLAND_DEDUP_PATH"] = real_dedup_path
        self.assertEqual(result, 0)

    def test_pretool_still_approves_bash_in_accept_edits_mode(self):
        relay = self.relay
        payloads = []
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_path = os.environ.get("VIBE_ISLAND_DEDUP_PATH")

        def fake_send(payload, wait_response=False):
            if not wait_response:
                payloads.append(payload)
                return ({"ok": True}, None)
            self.assertEqual(payload["request_kind"], "approval")
            return (None, "notch unavailable")

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /tmp/old"},
                "cwd": "/tmp/project",
            }
            os.environ["VIBE_ISLAND_DEDUP_PATH"] = tempfile.mktemp()
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            if real_dedup_path is None:
                os.environ.pop("VIBE_ISLAND_DEDUP_PATH", None)
            else:
                os.environ["VIBE_ISLAND_DEDUP_PATH"] = real_dedup_path
        self.assertTrue(any(p.get("request_kind") == "approval" for p in payloads))
        self.assertEqual(result, 0)

    def test_pretool_skips_approval_in_bypass_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_lock = relay.DEDUP_LOCK
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called in bypass mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-bypass",
                "permission_mode": "bypassPermissions",
                "tool_name": "Bash",
                "tool_input": {"command": "git push"},
                "cwd": "/tmp/project",
            }
            relay.DEDUP_LOCK = tempfile.mktemp(suffix=".lock")
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            relay.DEDUP_LOCK = real_dedup_lock
        self.assertEqual(result, 0)

    def test_pretool_skips_edit_approval_in_accept_edits_mode(self):
        relay = self.relay
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_lock = relay.DEDUP_LOCK
        try:
            relay.send = lambda *a, **kw: self.fail("send should not be called for edits in acceptEdits mode")
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/project/app.py"},
                "cwd": "/tmp/project",
            }
            relay.DEDUP_LOCK = tempfile.mktemp(suffix=".lock")
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            relay.DEDUP_LOCK = real_dedup_lock
        self.assertEqual(result, 0)

    def test_pretool_still_approves_bash_in_accept_edits_mode(self):
        relay = self.relay
        payloads = []
        real_send = relay.send
        real_read_event = relay.read_event
        real_dedup_lock = relay.DEDUP_LOCK

        def fake_send(payload, wait_response=False):
            if not wait_response:
                payloads.append(payload)
                return ({"ok": True}, None)
            self.assertEqual(payload["request_kind"], "approval")
            return (None, "notch unavailable")

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /tmp/old"},
                "cwd": "/tmp/project",
            }
            relay.DEDUP_LOCK = tempfile.mktemp(suffix=".lock")
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
            relay.DEDUP_LOCK = real_dedup_lock
        self.assertTrue(any(p.get("request_kind") == "approval" for p in payloads))
        self.assertEqual(result, 0)

    def _patch_dedup(self, relay):
        """Point dedup files to a writable temp path."""
        base = os.path.join(tempfile.gettempdir(), f"vibe-test-dedup-{os.getpid()}-{id(relay)}")
        relay.DEDUP_PATH = base + ".json"
        relay.DEDUP_LOCK = base + ".json.lock"

    def test_pretool_skips_approval_in_bypass_mode(self):
        relay = self.relay
        approval_sent = []
        real_send = relay.send
        real_read_event = relay.read_event

        def fake_send(payload, wait_response=False):
            if wait_response:
                approval_sent.append(payload)
            return ({"ok": True}, None)

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-bypass",
                "permission_mode": "bypassPermissions",
                "tool_name": "Bash",
                "tool_input": {"command": "git push"},
                "cwd": "/tmp/project",
            }
            self._patch_dedup(relay)
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        self.assertEqual(result, 0)
        self.assertEqual(approval_sent, [], "no approval should be requested in bypass mode")

    def test_pretool_skips_edit_approval_in_accept_edits_mode(self):
        relay = self.relay
        approval_sent = []
        real_send = relay.send
        real_read_event = relay.read_event

        def fake_send(payload, wait_response=False):
            if wait_response:
                approval_sent.append(payload)
            return ({"ok": True}, None)

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/project/app.py"},
                "cwd": "/tmp/project",
            }
            self._patch_dedup(relay)
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        self.assertEqual(result, 0)
        self.assertEqual(approval_sent, [], "no approval should be requested for edits in acceptEdits mode")

    def test_pretool_still_approves_bash_in_accept_edits_mode(self):
        relay = self.relay
        approval_sent = []
        real_send = relay.send
        real_read_event = relay.read_event

        def fake_send(payload, wait_response=False):
            if wait_response:
                approval_sent.append(payload)
            return ({"ok": True}, None)

        try:
            relay.send = fake_send
            relay.read_event = lambda: {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-accept",
                "permission_mode": "acceptEdits",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /tmp/old"},
                "cwd": "/tmp/project",
            }
            self._patch_dedup(relay)
            result = relay.main()
        finally:
            relay.send = real_send
            relay.read_event = real_read_event
        self.assertEqual(result, 0)
        self.assertTrue(
            any(p.get("request_kind") == "approval" for p in approval_sent),
            "Bash should still require approval in acceptEdits mode",
        )

    def test_collect_ask_updated_input_preserves_original_questions(self):
        relay = self.relay
        real_send = relay.send

        def fake_send(_payload, wait_response=False):
            self.assertTrue(wait_response)
            return (
                {
                    "request_id": "req-1",
                    "answers": {
                        "Who owns this?": ["Alice", "Bob"],
                    },
                },
                None,
            )

        relay.send = fake_send
        try:
            updated_input, error = relay.collect_ask_updated_input(
                event={
                    "session_id": "sess-1",
                    "_source": "claude",
                    "tool_input": {
                        "questions": [
                            {
                                "header": "owners",
                                "question": "Who owns this?",
                                "multiSelect": True,
                                "options": [{"label": "Alice"}, {"label": "Bob"}],
                            }
                        ]
                    },
                },
                agent_name="Claude Code",
                short_cwd="repo",
                request_id="req-1",
            )
        finally:
            relay.send = real_send

        self.assertIsNone(error)
        self.assertEqual(updated_input["questions"][0]["question"], "Who owns this?")
        self.assertEqual(updated_input["answers"], {"Who owns this?": "Alice, Bob"})

    def test_pretool_allow_prints_official_payload(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.relay.pretool_allow({"command": "git status"})

        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(payload["hookSpecificOutput"]["updatedInput"]["command"], "git status")

    def test_permission_decision_prints_official_payload(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.relay.permission_decision("allow", updated_input={"file_path": "foo.swift"})

        payload = json.loads(buffer.getvalue())
        decision = payload["hookSpecificOutput"]["decision"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(decision["updatedInput"]["file_path"], "foo.swift")

    def test_sign_and_verify_payload_use_canonical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = self.write_token(tmp)
            payload = {"message": "你好", "nested": {"beta": 2, "alpha": 1}}
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                signed = self.relay.sign_payload(payload)

            unsigned_payload = dict(signed)
            signature = unsigned_payload.pop("auth_signature")
            expected = hmac.new(
                bytes.fromhex("11" * 32),
                b'{"auth_nonce":"' + signed["auth_nonce"].encode("utf-8") + b'","message":"\xe4\xbd\xa0\xe5\xa5\xbd","nested":{"alpha":1,"beta":2}}',
                self.relay.hashlib.sha256,
            ).hexdigest()
            self.assertEqual(signature, expected)

            with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                self.assertIsNone(self.relay.verify_payload(signed))

    def test_verify_payload_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = self.write_token(tmp)
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                signed = self.relay.sign_payload({"request_id": "req-1", "action": "allow"})
                signed["action"] = "deny"
                self.assertEqual(self.relay.verify_payload(signed), "invalid auth_signature in response")

    def test_send_rejects_unsigned_forged_response(self):
        relay = self.relay
        real_socket = relay.socket.socket

        class FakeSocket:
            def settimeout(self, _timeout):
                return None

            def connect(self, _address):
                return None

            def sendall(self, _payload):
                return None

            def recv(self, _size):
                return b'{"request_id":"req-1","action":"allow"}\n'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        relay.socket.socket = lambda *args, **kwargs: FakeSocket()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_path = self.write_token(tmp)
                with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                    response, error = relay.send({"request_id": "req-1"}, wait_response=True)
        finally:
            relay.socket.socket = real_socket

        self.assertIsNone(response)
        self.assertEqual(error, "missing auth_nonce in response")

    def test_send_reports_invalid_json_response(self):
        relay = self.relay
        real_socket = relay.socket.socket

        class FakeSocket:
            def settimeout(self, _timeout):
                return None

            def connect(self, _address):
                return None

            def sendall(self, _payload):
                return None

            def recv(self, _size):
                return b"not-json\n"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        relay.socket.socket = lambda *args, **kwargs: FakeSocket()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_path = self.write_token(tmp)
                with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                    response, error = relay.send({"ping": True}, wait_response=True)
        finally:
            relay.socket.socket = real_socket

        self.assertIsNone(response)
        self.assertIn("invalid JSON", error)


class InstallHookScriptTests(unittest.TestCase):
    def test_install_and_uninstall_preserve_non_relay_hooks_and_set_matcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings_path = tmp_path / "settings.json"
            relay_events = [
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "PostToolUseFailure",
                "Stop",
                "StopFailure",
                "Notification",
                "PermissionRequest",
                "SessionEnd",
            ]
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /Users/old/Scripts/vibe-island/relay.py",
                                        }
                                    ],
                                }
                            ],
                            "Notification": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/existing_hook.py",
                                        }
                                    ]
                                }
                            ],
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/stop_hook.py",
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["HOME"] = tmp
            env["CLAUDE_SETTINGS"] = str(settings_path)

            subprocess.run(
                ["/bin/zsh", str(INSTALLER_PATH)],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            subprocess.run(
                ["/bin/zsh", str(INSTALLER_PATH)],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = cfg["hooks"]
            for event_name in relay_events:
                self.assertIn(event_name, hooks)

            pretool_entries = hooks["PreToolUse"]
            self.assertEqual(len(pretool_entries), 1)
            self.assertEqual(pretool_entries[0]["matcher"], "Bash|Edit|Write|NotebookEdit|AskUserQuestion")
            self.assertNotIn(
                "/Users/old/Scripts/vibe-island/relay.py",
                [
                    hook["command"]
                    for entry in pretool_entries
                    for hook in entry.get("hooks", [])
                ],
            )

            notification_commands = [
                hook["command"]
                for entry in hooks["Notification"]
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("python3 /tmp/existing_hook.py", notification_commands)
            self.assertEqual(
                sum("Scripts/vibe-island/relay.py" in command for command in notification_commands),
                1,
            )
            for event_name in relay_events:
                event_commands = [
                    hook["command"]
                    for entry in hooks[event_name]
                    for hook in entry.get("hooks", [])
                ]
                self.assertEqual(
                    sum("Scripts/vibe-island/relay.py" in command for command in event_commands),
                    1,
                    event_name,
                )

            stop_commands = [
                hook["command"]
                for entry in hooks["Stop"]
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("python3 /tmp/stop_hook.py", stop_commands)

            subprocess.run(
                ["/bin/zsh", str(INSTALLER_PATH), "--uninstall"],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = cfg["hooks"]
            notification_commands = [
                hook["command"]
                for entry in hooks["Notification"]
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(notification_commands, ["python3 /tmp/existing_hook.py"])
            stop_commands = [
                hook["command"]
                for entry in hooks["Stop"]
                for hook in entry.get("hooks", [])
            ]
            self.assertEqual(stop_commands, ["python3 /tmp/stop_hook.py"])
            self.assertNotIn("PermissionRequest", hooks)
            self.assertNotIn("SessionEnd", hooks)
            self.assertNotIn("PostToolUseFailure", hooks)
            self.assertNotIn("StopFailure", hooks)


if __name__ == "__main__":
    unittest.main()
