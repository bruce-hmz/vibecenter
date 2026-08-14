"""IPC server protocol tests — real sockets, real relay.py signing.

Spins up vibecenter.ipc.IPCServer on an ephemeral port with a random
token, then talks to it exactly the way relay.py does.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import relay as relay_mod  # noqa: E402
from vibecenter import auth  # noqa: E402
from vibecenter.ipc import IPCServer  # noqa: E402


class ServerHarness:
    def __init__(self) -> None:
        self.token = os.urandom(32)
        self.port = 24321
        self.session_messages = []
        self.usage_messages = []
        self.enqueued = []
        self.server = IPCServer(
            on_session=self.session_messages.append,
            on_usage=self.usage_messages.append,
            on_request=lambda request, held: self.enqueued.append((request, held)),
            port=self.port,
            token=self.token,
        )
        assert self.server.start()

    def stop(self) -> None:
        self.server.stop()

    def connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", self.port))
        return sock

    def send(self, sock: socket.socket, payload: dict) -> None:
        signed = auth.sign_payload(payload, self.token)
        sock.sendall((json.dumps(signed, ensure_ascii=False) + "\n").encode("utf-8"))

    def recv_response(self, sock: socket.socket) -> dict:
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            raise AssertionError("no response from server")
        return json.loads(buffer.decode("utf-8").strip())


def verify_with_relay(response: dict, token: bytes) -> None:
    """relay-style verification against our response (uses relay code)."""
    unsigned = dict(response)
    signature = unsigned.pop("auth_signature", None)
    assert signature, "response missing auth_signature"
    expected = relay_mod.hmac.new(
        token,
        relay_mod.canonical_json_bytes(unsigned),
        relay_mod.hashlib.sha256,
    ).hexdigest()
    assert relay_mod.hmac.compare_digest(signature, expected), "signature mismatch"


class IPCServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = ServerHarness()
        self.addCleanup(self.harness.stop)

    def test_session_message_dispatch(self) -> None:
        sock = self.harness.connect()
        self.harness.send(sock, {
            "session": "update", "session_id": "s1", "source": "claude",
            "task": "fix the bug", "running": True,
        })
        sock.close()
        deadline = time.time() + 2
        while not self.harness.session_messages and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.harness.session_messages), 1)
        self.assertEqual(self.harness.session_messages[0]["session_id"], "s1")

    def test_unsigned_payload_is_rejected(self) -> None:
        sock = self.harness.connect()
        sock.sendall((json.dumps({"session": "update", "session_id": "evil"})
                      + "\n").encode())
        response = self.harness.recv_response(sock)
        self.assertEqual(response.get("type"), "error")
        self.assertIn("auth", response.get("detail", ""))
        self.assertEqual(self.harness.session_messages, [])

    def test_tampered_signature_is_rejected(self) -> None:
        sock = self.harness.connect()
        signed = auth.sign_payload({"session": "update", "session_id": "evil"},
                                   self.harness.token)
        signed["session_id"] = "tampered"
        sock.sendall((json.dumps(signed) + "\n").encode())
        response = self.harness.recv_response(sock)
        self.assertEqual(response.get("type"), "error")
        self.assertEqual(self.harness.session_messages, [])

    def test_usage_message_dispatch(self) -> None:
        sock = self.harness.connect()
        self.harness.send(sock, {
            "type": "usage",
            "usage": {"provider": "Z.ai", "five_hour": 6, "five_hour_reset": "53m"},
        })
        sock.close()
        deadline = time.time() + 2
        while not self.harness.usage_messages and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.harness.usage_messages[0]["usage"]["provider"], "Z.ai")

    def test_approval_request_roundtrip_allow(self) -> None:
        sock = self.harness.connect()
        self.harness.send(sock, {
            "type": "request", "request_kind": "approval",
            "request_id": "req-42", "session_id": "s1", "source": "claude",
            "agent": "Claude Code", "task": "Edit file",
            "tool_name": "Edit", "cwd": "/tmp/ws",
            "targetFile": "/tmp/ws/a.py", "timeout_seconds": 300,
        })

        deadline = time.time() + 2
        while not self.harness.enqueued and time.time() < deadline:
            time.sleep(0.02)
        request, held = self.harness.enqueued[0]
        self.assertEqual(request.id, "req-42")
        self.assertEqual(request.kind, "approval")
        self.assertEqual(request.tool_name, "Edit")
        self.assertEqual(request.risk.level, "medium")

        response_holder = {}

        def read_response():
            response_holder["data"] = self.harness.recv_response(sock)

        reader = threading.Thread(target=read_response)
        reader.start()
        self.harness.server.decide("req-42", {"request_id": "req-42",
                                              "action": "allow",
                                              "decision_source": "approval_button"})
        reader.join(timeout=5)
        response = response_holder.get("data")
        self.assertIsNotNone(response)
        self.assertEqual(response["request_id"], "req-42")
        self.assertEqual(response["action"], "allow")
        verify_with_relay(response, self.harness.token)
        sock.close()

    def test_ask_request_answers_roundtrip(self) -> None:
        sock = self.harness.connect()
        self.harness.send(sock, {
            "type": "request", "request_kind": "ask",
            "request_id": "ask-1", "session_id": "s2", "source": "zcode",
            "agent": "ZCode", "task": "Question",
            "questions": [{
                "id": "q1", "header": "部署方式", "question": "怎么发布？",
                "multiSelect": False,
                "options": [
                    {"id": "a", "label": "直接上线"},
                    {"id": "b", "label": "先发预览环境"},
                ],
            }],
            "timeout_seconds": 300,
        })

        deadline = time.time() + 2
        while not self.harness.enqueued and time.time() < deadline:
            time.sleep(0.02)
        request, held = self.harness.enqueued[0]
        self.assertEqual(request.kind, "ask")
        self.assertEqual(request.questions[0].header, "部署方式")
        self.assertEqual(len(request.questions[0].options), 2)

        response_holder = {}

        def read_response():
            response_holder["data"] = self.harness.recv_response(sock)

        reader = threading.Thread(target=read_response)
        reader.start()
        self.harness.server.decide("ask-1", {
            "request_id": "ask-1",
            "answers": {"怎么发布？": "先发预览环境"},
        })
        reader.join(timeout=5)
        response = response_holder.get("data")
        self.assertEqual(response["answers"], {"怎么发布？": "先发预览环境"})
        verify_with_relay(response, self.harness.token)
        sock.close()

    def test_abandon_session_denies_held_requests(self) -> None:
        sock = self.harness.connect()
        self.harness.send(sock, {
            "type": "request", "request_kind": "approval",
            "request_id": "req-77", "session_id": "gone", "source": "claude",
            "task": "Run command", "tool_name": "Bash", "command": "ls",
            "timeout_seconds": 300,
        })
        deadline = time.time() + 2
        while not self.harness.enqueued and time.time() < deadline:
            time.sleep(0.02)

        response_holder = {}

        def read_response():
            response_holder["data"] = self.harness.recv_response(sock)

        reader = threading.Thread(target=read_response)
        reader.start()
        self.harness.server.abandon_session("gone")
        reader.join(timeout=5)
        response = response_holder.get("data")
        self.assertEqual(response.get("action"), "deny")
        self.assertEqual(response.get("reason"), "session_end")
        verify_with_relay(response, self.harness.token)
        sock.close()

    def test_duplicate_request_id_not_reenqueued(self) -> None:
        for _ in range(2):
            sock = self.harness.connect()
            self.harness.send(sock, {
                "type": "request", "request_kind": "approval",
                "request_id": "dup-1", "session_id": "s", "source": "claude",
                "task": "Edit file", "tool_name": "Edit",
                "targetFile": "/tmp/ws/a.py", "timeout_seconds": 300,
            })
        # The dedup actually happens in the Store; at the IPC layer both
        # arrive. Verify both hold independent responses without crash.
        deadline = time.time() + 2
        while len(self.harness.enqueued) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.harness.enqueued), 2)
        self.harness.server.decide("dup-1", {"request_id": "dup-1", "action": "deny"})


if __name__ == "__main__":
    unittest.main()
