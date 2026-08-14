"""Authenticated TCP IPC server — port of the Swift LocalServer.

Listens on 127.0.0.1:14321 (same port as the macOS app) and speaks the
relay.py protocol: newline-delimited signed JSON. Non-blocking events
(session updates, usage pushes) are dispatched to callbacks; approval /
ask requests hold the connection open until the UI decides or the
fail-closed timeout denies.

The Qt layer connects via IPCBridge signals (queued across threads).
"""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from .auth import load_or_create_token, sign_payload, verify_payload
from .models import AskOption, AskQuestion, PendingRequest

HOST = "127.0.0.1"
PORT = 14321
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def _parse_questions(raw: Any) -> Tuple[list, str]:
    """Normalize relay questions payloads into AskQuestion models."""
    if not isinstance(raw, list) or not raw:
        return [], "questions payload missing"
    questions = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        header = str(item.get("header") or item.get("id") or f"question_{index + 1}").strip()
        text = str(item.get("question") or item.get("prompt") or header).strip()
        options = []
        for option_index, raw_option in enumerate(item.get("options") or []):
            if isinstance(raw_option, dict):
                label = str(raw_option.get("label") or "").strip() or f"Option {option_index + 1}"
                option_id = str(raw_option.get("id") or label).strip() or label
                description = str(raw_option.get("description") or "").strip()
            else:
                label = str(raw_option).strip() or f"Option {option_index + 1}"
                option_id, description = label, ""
            options.append(AskOption(id=option_id, label=label, description=description))
        if not options:
            return [], f"question '{header}' has no options"
        questions.append(AskQuestion(
            id=str(item.get("id") or header).strip() or header,
            header=header, question=text,
            multi_select=bool(item.get("multiSelect") or item.get("multi_select")),
            options=options,
        ))
    if not questions:
        return [], "questions payload missing"
    return questions, ""


class HeldRequest:
    """An in-flight approval/ask waiting for a UI decision."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None
        self.payload = payload

    def decide(self, response: Dict[str, Any]) -> None:
        self.response = response
        self.event.set()


class IPCServer:
    """Threaded TCP server. All callbacks run on server threads."""

    def __init__(
        self,
        on_session: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_compact: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_usage: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_usage_status: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_request: Optional[Callable[[PendingRequest, HeldRequest], None]] = None,
        on_client_closed: Optional[Callable[[str], None]] = None,
        port: int = PORT,
        token: Optional[bytes] = None,
    ) -> None:
        self.on_session = on_session
        self.on_compact = on_compact
        self.on_usage = on_usage
        self.on_usage_status = on_usage_status
        self.on_request = on_request
        self.on_client_closed = on_client_closed
        self.port = port
        self.token = token if token is not None else load_or_create_token()
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._held: Dict[str, HeldRequest] = {}
        self._lock = threading.Lock()
        self.last_error = ""

    # ── lifecycle ─────────────────────────────────────────

    def start(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, self.port))
            sock.listen(16)
            sock.settimeout(0.5)
        except OSError as exc:
            self.last_error = str(exc)
            return False
        self._server_sock = sock
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="vibecenter-ipc")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            held = list(self._held.values())
            self._held.clear()
        for item in held:
            item.decide({"request_id": "", "action": "deny",
                         "reason": "server_stopping"})
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                assert self._server_sock is not None
                client, _addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_client, args=(client,),
                             daemon=True).start()

    # ── per-client loop ───────────────────────────────────

    def _serve_client(self, client: socket.socket) -> None:
        client_id = uuid.uuid4().hex
        client.settimeout(1.0)
        buffer = b""
        held_ids = []
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_MESSAGE_BYTES:
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = self._handle_line(line)
                    if reply is None:
                        continue
                    if isinstance(reply, HeldRequest):
                        held_ids.append(reply.payload.get("request_id") or "")
                        if self._wait_and_respond(client, reply):
                            # Connection served its request; relay closes.
                            return
                        continue
                    client.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            with self._lock:
                for request_id in held_ids:
                    self._held.pop(request_id, None)
            try:
                client.close()
            except OSError:
                pass
            if self.on_client_closed:
                self.on_client_closed(client_id)

    def _wait_and_respond(self, client: socket.socket, held: HeldRequest) -> bool:
        """Block until decided (or fail-closed timeout), then reply."""
        timeout = min(max(float(held.payload.get("timeout_seconds") or 300), 15), 600)
        decided = held.event.wait(timeout + 2)
        response = held.response or {
            "request_id": held.payload.get("request_id") or "",
            "action": "deny",
            "decision_source": "timeout",
        }
        payload = {k: v for k, v in response.items()}
        payload.setdefault("request_id", held.payload.get("request_id") or "")
        try:
            client.sendall(
                json.dumps(sign_payload(payload, self.token), ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
        except OSError:
            pass
        return decided or response.get("action") == "deny"

    # ── message handling ──────────────────────────────────

    def _handle_line(self, line: bytes):
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"type": "error", "detail": "invalid JSON"}
        if not isinstance(message, dict):
            return {"type": "error", "detail": "payload must be an object"}
        ok, error = verify_payload(message, self.token)
        if not ok:
            return {"type": "error", "detail": f"auth failed: {error}"}

        if message.get("type") == "usage":
            if self.on_usage:
                self.on_usage(message)
            return None
        if message.get("type") == "usage_status":
            if self.on_usage_status:
                self.on_usage_status(message)
            return None

        state = message.get("state") or (
            message.get("request_kind") if message.get("type") == "request" else None
        )
        if state in ("approval", "ask"):
            return self._enqueue_request(message, state)

        if message.get("session"):
            if self.on_session:
                self.on_session(message)
            return None

        if message.get("state") == "compact":
            if self.on_compact:
                self.on_compact(message)
            return None
        return None

    def _enqueue_request(self, message: Dict[str, Any], kind: str) -> Optional[HeldRequest]:
        request_id = str(message.get("request_id") or "").strip() or uuid.uuid4().hex
        questions, error = _parse_questions(message.get("questions"))
        if kind == "ask" and error:
            if error == "questions payload missing":
                # Legacy single-question shape from older relays.
                labels = [str(o) for o in (message.get("options") or [])]
                if labels:
                    prompt = str(message.get("task") or "Question")
                    questions = [AskQuestion(
                        id="question-1", header="Question", question=prompt,
                        options=[AskOption(id=label, label=label) for label in labels],
                    )]
        if kind == "ask" and not questions:
            # Malformed ask — deny so the agent isn't blocked forever.
            held = HeldRequest(message)
            held.decide({"request_id": request_id, "action": "deny",
                         "reason": "ask request has no selectable options"})
            return held

        target = str(message.get("targetFile") or message.get("target_file") or "")
        try:
            timeout_seconds = min(max(float(message.get("timeout_seconds") or 300), 15), 600)
        except (TypeError, ValueError):
            timeout_seconds = 300
        request = PendingRequest(
            id=request_id,
            kind=kind,
            session_id=str(message.get("session_id") or ""),
            source=str(message.get("source") or "claude"),
            agent_name=str(message.get("agent") or "Claude Code"),
            task_name=str(message.get("task") or ("Permission request" if kind == "approval" else "Question")),
            target_file=target,
            tool_name=str(message.get("tool_name") or message.get("toolName") or ""),
            command=str(message.get("command") or "") if kind == "approval" else "",
            cwd=str(message.get("cwd") or ""),
            reason=str(message.get("reason") or ""),
            diff=str(message.get("diff") or ""),
            questions=questions,
            arrived_at=time.time(),
            expires_at=time.time() + timeout_seconds,
        )
        held = HeldRequest(message)
        held.payload["request_id"] = request_id
        with self._lock:
            self._held[request_id] = held
        if self.on_request:
            self.on_request(request, held)
        return held

    # ── decisions (called from the UI thread) ─────────────

    def decide(self, request_id: str, response: Dict[str, Any]) -> None:
        with self._lock:
            held = self._held.pop(request_id, None)
        if held:
            held.decide(response)

    def abandon_session(self, session_id: str) -> None:
        """Deny (without decision history) requests whose session ended."""
        with self._lock:
            held = list(self._held.values())
        for item in held:
            if str(item.payload.get("session_id") or "") == session_id:
                self.decide(str(item.payload.get("request_id") or ""),
                            {"request_id": item.payload.get("request_id") or "",
                             "action": "deny", "reason": "session_end"})
