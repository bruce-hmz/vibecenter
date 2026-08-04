#!/usr/bin/env python3
"""
vibe-island-relay — Claude Code hook → self-made Vibe Island notch app.

Non-blocking hook events update the notch UI. Blocking flows
(`PreToolUse` writes, `PermissionRequest`, and `AskUserQuestion`) wait for a
UI response and deny the request if the app is unavailable or responds with
invalid data.
"""

from __future__ import annotations

import fcntl
import json
import hashlib
import hmac
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any

HOST = "127.0.0.1"
PORT = 14321
TIMEOUT_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 300
MAX_RESPONSE_BYTES = 1024 * 1024
WRITE_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}
DEFAULT_IPC_TOKEN_FILE = "~/.vibe-island/run/ipc-token"

# Tools that mutate files. In acceptEdits mode Claude Code auto-approves
# these, so the relay should not second-guess that decision.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Permission modes where the user has expressed intent to skip prompts.
# PreToolUse hooks fire before the permission-mode check, so the relay must
# inspect this field itself to avoid re-prompting what the user already
# bypassed.
def is_bypass_mode(event: dict[str, Any]) -> bool:
    return str(event.get("permission_mode") or "").strip() == "bypassPermissions"


def is_edit_accepted_mode(event: dict[str, Any]) -> bool:
    return str(event.get("permission_mode") or "").strip() == "acceptEdits"


def truncate(value: str | None, limit: int = 80) -> str:
    text = (value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_event() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid hook JSON: {exc.msg}") from exc
    if not isinstance(event, dict):
        raise ValueError(f"Hook payload must be an object, got {type(event).__name__}")
    return event


def infer_source(event: dict[str, Any]) -> str:
    source = str(event.get("_source") or "").strip().lower()
    if source:
        return source
    if os.environ.get("ZCODE_SESSION_ID") or os.environ.get("ZCODE_CLI"):
        return "zcode"
    if "zcode" in os.environ.get("TERM_PROGRAM", "").lower():
        return "zcode"
    return "claude"


def agent_name_for(source: str) -> str:
    return {
        "claude": "Claude Code",
        "codex": "Codex",
        "zcode": "ZCode",
    }.get(source, source.capitalize() or "Agent")


def session_id_for(event: dict[str, Any]) -> str:
    return str(event.get("session_id") or event.get("sessionId") or "").strip()


def tool_name_for(event: dict[str, Any]) -> str:
    return str(event.get("tool_name") or event.get("toolName") or "").strip()


def tool_input_for(event: dict[str, Any]) -> dict[str, Any]:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        return tool_input
    tool_input = event.get("toolInput")
    if isinstance(tool_input, dict):
        return tool_input
    return {}


def request_id_for(event: dict[str, Any], event_name: str, tool_name: str) -> str:
    existing = str(
        event.get("request_id")
        or event.get("requestId")
        or event.get("tool_use_id")
        or event.get("toolUseId")
        or ""
    ).strip()
    if existing:
        return existing
    session_id = session_id_for(event) or "sessionless"
    identity = {
        "session_id": session_id,
        "event_name": event_name,
        "tool_name": tool_name,
        "tool_input": tool_input_for(event),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"{session_id}:{event_name}:{tool_name}:{digest}"


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def token_file_path() -> str:
    return os.path.expanduser(os.environ.get("VIBE_ISLAND_IPC_TOKEN_FILE", DEFAULT_IPC_TOKEN_FILE))


def load_token() -> bytes:
    path = token_file_path()
    try:
        with open(path, encoding="utf-8") as handle:
            token_hex = handle.read().strip()
    except FileNotFoundError as exc:
        raise ValueError(f"ipc token file missing: {path}") from exc
    except OSError as exc:
        raise ValueError(f"ipc token file unreadable: {exc}") from exc

    if len(token_hex) != 64:
        raise ValueError("ipc token file must contain exactly 64 hex characters")
    try:
        token = bytes.fromhex(token_hex)
    except ValueError as exc:
        raise ValueError("ipc token file must contain exactly 64 hex characters") from exc
    if len(token) != 32:
        raise ValueError("ipc token file must decode to 32 bytes")
    return token


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    signed_payload = dict(payload)
    signed_payload["auth_nonce"] = uuid.uuid4().hex
    unsigned_payload = dict(signed_payload)
    signed_payload["auth_signature"] = hmac.new(
        load_token(),
        canonical_json_bytes(unsigned_payload),
        hashlib.sha256,
    ).hexdigest()
    return signed_payload


def verify_payload(payload: dict[str, Any]) -> str | None:
    nonce = str(payload.get("auth_nonce") or "").strip()
    if not nonce:
        return "missing auth_nonce in response"

    signature = str(payload.get("auth_signature") or "").strip()
    if not signature:
        return "missing auth_signature in response"

    unsigned_payload = dict(payload)
    unsigned_payload.pop("auth_signature", None)
    expected_signature = hmac.new(
        load_token(),
        canonical_json_bytes(unsigned_payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return "invalid auth_signature in response"
    return None


def send(payload: dict[str, Any], wait_response: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    try:
        signed_payload = sign_payload(payload)
    except ValueError as exc:
        return None, f"ipc auth unavailable: {exc}"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
            conn.settimeout(TIMEOUT_SECONDS)
            conn.connect((HOST, PORT))
            conn.sendall((json.dumps(signed_payload) + "\n").encode("utf-8"))
            if not wait_response:
                return {"ok": True}, None

            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_RESPONSE_BYTES:
                    return None, "response from notch app exceeds 1 MiB"
    except (ConnectionRefusedError, TimeoutError, socket.timeout, OSError) as exc:
        return None, f"socket failure: {exc}"

    if not buffer:
        return None, "empty response from notch app"

    try:
        response = json.loads(buffer.decode("utf-8").strip())
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON response: {exc.msg}"

    if not isinstance(response, dict):
        return None, f"response must be an object, got {type(response).__name__}"
    try:
        auth_error = verify_payload(response)
    except ValueError as exc:
        return None, f"ipc auth unavailable: {exc}"
    if auth_error:
        return None, auth_error
    return response, None


def push_session(
    event: dict[str, Any],
    action: str,
    task: str,
    *,
    detail: str | None = None,
    running: bool | None = None,
    event_kind: str | None = None,
) -> None:
    session_id = session_id_for(event)
    if not session_id:
        return
    if event_kind is not None and event_kind not in {"completed", "failed", "waiting"}:
        raise ValueError(f"invalid event_kind: {event_kind}")
    payload: dict[str, Any] = {
        "session": action,
        "session_id": session_id,
        "source": infer_source(event),
        "task": task,
    }
    # Ordinary live-state updates may refresh the in-app workspace context.
    # Attention events deliberately omit it so their notification envelope
    # contains only the minimum routing metadata.
    if event_kind is None:
        cwd = str(event.get("cwd") or "")
        if cwd:
            payload["cwd"] = cwd
    if detail:
        payload["detail"] = detail
    if running is not None:
        payload["running"] = running
    if event_kind is not None:
        payload["event_kind"] = event_kind
    send(payload)


def send_compact_state(agent_name: str, task: str, target_file: str = "") -> None:
    send({
        "state": "compact",
        "agent": agent_name,
        "task": task,
        "targetFile": target_file,
    })


def describe_tool(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str, str]:
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        return "Run command", truncate(command, 80), command
    if tool_name == "Edit":
        path = str(tool_input.get("file_path") or "")
        return "Edit file", truncate(path, 80), path
    if tool_name == "Write":
        path = str(tool_input.get("file_path") or "")
        return "Write file", truncate(path, 80), path
    if tool_name == "NotebookEdit":
        path = str(tool_input.get("notebook_path") or "")
        return "Edit notebook", truncate(path, 80), path
    if tool_name == "Read":
        path = str(tool_input.get("file_path") or "")
        return "Read file", truncate(path, 80), path
    if tool_name == "AskUserQuestion":
        return "Question", "Agent needs input", "Agent needs input"
    detail = json.dumps(tool_input, ensure_ascii=False)
    return tool_name or "Tool", truncate(detail, 80), detail


def normalize_option(raw_option: Any, option_index: int) -> dict[str, str]:
    if isinstance(raw_option, dict):
        label = str(raw_option.get("label") or "").strip() or f"Option {option_index + 1}"
        option_id = str(raw_option.get("id") or label).strip() or label
        description = str(raw_option.get("description") or "").strip()
        return {"id": option_id, "label": label, "description": description}
    label = str(raw_option).strip() or f"Option {option_index + 1}"
    return {"id": label, "label": label, "description": ""}


def normalize_question(raw_question: dict[str, Any], index: int) -> dict[str, Any]:
    header = str(raw_question.get("header") or raw_question.get("id") or f"question_{index + 1}").strip()
    question = str(raw_question.get("question") or raw_question.get("prompt") or header).strip()
    raw_options = raw_question.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise ValueError(f"AskUserQuestion question '{header}' has no options")
    options = [normalize_option(option, option_index) for option_index, option in enumerate(raw_options)]
    return {
        "id": str(raw_question.get("id") or header).strip() or header,
        "header": header,
        "question": question,
        "multiSelect": bool(raw_question.get("multiSelect")),
        "options": options,
    }


def build_ask_payload(*, event: dict[str, Any], request_id: str, agent_name: str, short_cwd: str, normalized_questions: list[dict[str, Any]]) -> dict[str, Any]:
    first = normalized_questions[0]
    return {
        "type": "request",
        "request_kind": "ask",
        "request_id": request_id,
        "session_id": session_id_for(event),
        "source": infer_source(event),
        "agent": agent_name,
        "task": "Question",
        "cwd": str(event.get("cwd") or ""),
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "prompt": first["question"],
        "targetFile": short_cwd,
        "detail": first["question"],
        "options": [option["label"] for option in first["options"]],
        "questions": normalized_questions,
    }


def parse_ask_answers(response: dict[str, Any], request_id: str, questions: list[dict[str, Any]]) -> tuple[dict[str, str] | None, str | None]:
    response_request_id = str(response.get("request_id") or "").strip()
    if response_request_id != request_id:
        return None, (
            f"response request_id mismatch: expected {request_id}, "
            f"got {response_request_id or '<missing>'}"
        )

    if str(response.get("action") or "").strip().lower() == "cancel":
        reason = str(response.get("reason") or "user_cancelled").strip()
        return None, f"cancelled:{reason}"

    raw_answers = response.get("answers")
    if isinstance(raw_answers, dict) and raw_answers:
        normalized: dict[str, str] = {}
        for question in questions:
            value = raw_answers.get(question["id"])
            if value is None:
                value = raw_answers.get(question["question"])
            if value is None:
                value = raw_answers.get(question["header"])
            if value is None:
                return None, f"missing answer for question '{question['question']}'"
            values = value if isinstance(value, list) else [value]
            labels = [str(item).strip() for item in values if str(item).strip()]
            allowed = {option["label"] for option in question["options"]}
            if not labels or any(label not in allowed for label in labels):
                return None, f"invalid answer for question '{question['question']}': {value!r}"
            normalized[question["question"]] = ", ".join(labels) if question.get("multiSelect") else labels[0]
        return normalized, None

    selected = str(response.get("selected_option") or "").strip()
    if not selected:
        return None, "no answer returned from notch app"
    first = questions[0]
    allowed = {option["label"] for option in first["options"]}
    if selected not in allowed:
        return None, f"invalid AskUserQuestion answer: {selected}"
    return {first["question"]: selected}, None


def collect_ask_updated_input(*, event: dict[str, Any], agent_name: str, short_cwd: str, request_id: str) -> tuple[dict[str, Any] | None, str | None]:
    raw_questions = tool_input_for(event).get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None, "AskUserQuestion missing tool_input.questions"
    try:
        normalized_questions = [normalize_question(raw_question, index) for index, raw_question in enumerate(raw_questions)]
    except ValueError as exc:
        return None, str(exc)

    payload = build_ask_payload(
        event=event,
        request_id=request_id,
        agent_name=agent_name,
        short_cwd=short_cwd,
        normalized_questions=normalized_questions,
    )
    response, error = send(payload, wait_response=True)
    if error:
        return None, error
    answers, error = parse_ask_answers(response or {}, request_id, normalized_questions)
    if error:
        return None, error
    return {
        "questions": raw_questions,
        "answers": answers,
    }, None


def pretool_allow(updated_input: dict[str, Any]) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }))


def pretool_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def permission_decision(behavior: str, *, message: str | None = None, updated_input: dict[str, Any] | None = None) -> None:
    decision: dict[str, Any] = {"behavior": behavior}
    if message:
        decision["message"] = message
    if updated_input:
        decision["updatedInput"] = updated_input
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }))


def handle_ask_user_question(event: dict[str, Any], agent_name: str, short_cwd: str) -> int:
    request_id = request_id_for(event, "PreToolUse", "AskUserQuestion")
    updated_input, error = collect_ask_updated_input(
        event=event,
        agent_name=agent_name,
        short_cwd=short_cwd,
        request_id=request_id,
    )
    if error:
        if error.startswith("cancelled:"):
            reason = error.split(":", 1)[1] or "user_cancelled"
            pretool_deny(f"User cancelled AskUserQuestion: {reason}")
        else:
            pretool_deny(f"Vibe Island AskUserQuestion unavailable: {error}")
        return 0
    pretool_allow(updated_input or {})
    return 0


def approval_diff(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    if tool_name != "Edit":
        return None
    before = str(tool_input.get("old_string") or "")
    after = str(tool_input.get("new_string") or "")
    if not before and not after:
        return None
    diff = f"--- before\n{before}\n+++ after\n{after}"
    limit = 12000
    return diff if len(diff) <= limit else diff[:limit] + "\n… [context truncated]"


# A single write tool call triggers BOTH PreToolUse and PermissionRequest,
# and since relay runs as a fresh process per hook event there's no in-process
# memory between them. This file-based store dedups by content so the user is
# asked once; the second event replays the first's decision instead of making
# a duplicate approval row.
DEDUP_PATH = os.path.expanduser(
    os.environ.get("VIBE_ISLAND_DEDUP_PATH", "~/.vibe-island/run/approval-dedup.json")
)
DEDUP_LOCK = DEDUP_PATH + ".lock"
DEDUP_PENDING_TTL = 600.0   # a pending claim survives while the user decides
DEDUP_DECIDED_TTL = 60.0    # a remembered decision is replayed only briefly


def _dedup_key(event: dict[str, Any]) -> str:
    # Prefer the tool-use id: it is identical across PreToolUse and
    # PermissionRequest for the SAME tool call, and unique per call, so it
    # collapses the double-fire without merging distinct writes.
    stable = str(event.get("tool_use_id") or event.get("toolUseId")
                 or event.get("request_id") or event.get("requestId") or "").strip()
    if stable:
        return "id:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    # Fallback: tool + target (file path / command) — describe_tool renders
    # these identically for both events even when the raw input shape differs.
    tool = tool_name_for(event) or ""
    _, target, _ = describe_tool(tool, tool_input_for(event))
    payload = json.dumps({"tool": tool, "target": (target or "").strip()},
                         sort_keys=True, ensure_ascii=False)
    return "tt:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@contextmanager
def _dedup_locked():
    os.makedirs(os.path.dirname(DEDUP_LOCK) or ".", exist_ok=True)
    lock = open(DEDUP_LOCK, "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _dedup_read() -> dict[str, Any]:
    try:
        with open(DEDUP_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _dedup_write(data: dict[str, Any]) -> None:
    tmp = DEDUP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, DEDUP_PATH)


def dedup_remember(key: str, decision: str) -> None:
    with _dedup_locked():
        data = _dedup_read()
        data[key] = {"state": "decided", "decision": decision, "ts": time.time()}
        _dedup_write(data)


def dedup_clear(key: str) -> None:
    with _dedup_locked():
        data = _dedup_read()
        if key in data:
            del data[key]
            _dedup_write(data)


def dedup_claim_or_replay(key: str) -> str | None:
    """Under lock: replay a fresh decision, or claim the prompting role.

    Returns "allow"/"deny" (replay an existing decision), "prompt" (we own it),
    or "wait" (another process is prompting; caller should poll).
    """
    with _dedup_locked():
        data = _dedup_read()
        entry = data.get(key)
        now = time.time()
        if entry:
            age = now - entry.get("ts", 0)
            if entry.get("state") == "decided" and age <= DEDUP_DECIDED_TTL:
                return entry.get("decision")
            if entry.get("state") == "pending" and age <= DEDUP_PENDING_TTL:
                return "wait"
        data[key] = {"state": "pending", "ts": now}
        _dedup_write(data)
        return "prompt"


def dedup_wait_for_decision(key: str, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _dedup_locked():
            entry = _dedup_read().get(key)
        if entry and entry.get("state") == "decided" and \
                (time.time() - entry.get("ts", 0)) <= DEDUP_DECIDED_TTL:
            return entry.get("decision")
        time.sleep(0.4)
    return None


def request_approval_dedup(event: dict[str, Any], agent_name: str,
                           event_name: str) -> tuple[str | None, str | None]:
    """request_approval wrapped so PreToolUse and PermissionRequest for the
    same write collapse into a single user prompt."""
    key = _dedup_key(event)
    try:
        verdict = dedup_claim_or_replay(key)
    except OSError as exc:
        return None, f"approval dedup unavailable: {exc}"
    if verdict in ("allow", "deny"):
        return verdict, None
    if verdict == "wait":
        decision = dedup_wait_for_decision(key, TIMEOUT_SECONDS)
        if decision in ("allow", "deny"):
            return decision, None
        # prompter died/timed out — fall through to prompt ourselves
    action, error = request_approval(event, agent_name, event_name)
    if error:
        try:
            dedup_clear(key)
        except OSError:
            pass
        return None, error
    try:
        dedup_remember(key, action)
    except OSError:
        # The user already made an explicit decision. A cache write failure
        # must not silently reverse it; only the duplicate event may prompt
        # again, which is safer than granting without consent.
        pass
    return action, None


def request_approval(event: dict[str, Any], agent_name: str, event_name: str) -> tuple[str | None, str | None]:
    tool_name = tool_name_for(event)
    tool_input = tool_input_for(event)
    task, target, detail = describe_tool(tool_name, tool_input)
    request_id = request_id_for(event, event_name, tool_name)
    response, error = send({
        "type": "request",
        "request_kind": "approval",
        "request_id": request_id,
        "session_id": session_id_for(event),
        "source": infer_source(event),
        "agent": agent_name,
        "task": task,
        "targetFile": target,
        "tool_name": tool_name,
        "command": detail if tool_name == "Bash" else None,
        "cwd": str(event.get("cwd") or ""),
        "reason": str(tool_input.get("description") or ""),
        "diff": approval_diff(tool_name, tool_input),
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    }, wait_response=True)
    if error:
        return None, error

    response = response or {}
    response_request_id = str(response.get("request_id") or "").strip()
    if response_request_id != request_id:
        return None, (
            f"response request_id mismatch: expected {request_id}, "
            f"got {response_request_id or '<missing>'}"
        )
    action = str(response.get("action") or "").strip().lower()
    if action not in {"allow", "deny"}:
        return None, f"invalid approval action: {action or '<missing>'}"
    return action, None


def handle_pretool_approval(event: dict[str, Any], agent_name: str) -> int:
    tool_name = tool_name_for(event)
    task, target, _ = describe_tool(tool_name, tool_input_for(event))
    action, error = request_approval_dedup(event, agent_name, "PreToolUse")
    if error:
        pretool_deny(f"Vibe Island approval unavailable: {error}")
        return 0
    if action == "allow":
        pretool_allow(tool_input_for(event))
        return 0
    pretool_deny(f"User denied {task}: {target}")
    return 0


def handle_permission_request(event: dict[str, Any], agent_name: str, short_cwd: str) -> int:
    tool_name = tool_name_for(event)
    task, target, _ = describe_tool(tool_name, tool_input_for(event))
    action, error = request_approval_dedup(event, agent_name, "PermissionRequest")
    if error:
        permission_decision("deny", message=f"Vibe Island approval unavailable: {error}")
        return 0

    if action == "allow":
        permission_decision("allow", updated_input=tool_input_for(event))
        return 0

    permission_decision("deny", message=f"User denied {task}: {target or short_cwd}")
    return 0


def main() -> int:
    try:
        event = read_event()
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "").strip()
    tool_name = tool_name_for(event)
    cwd = str(event.get("cwd") or os.getcwd()).strip()
    short_cwd = os.path.basename(cwd.rstrip("/")) or cwd
    agent_name = agent_name_for(infer_source(event))

    if event_name == "SessionStart":
        push_session(event, "start", "Session start", detail=short_cwd, running=False)
        send_compact_state(agent_name, "Session start", short_cwd)
        return 0

    if event_name == "SessionEnd":
        push_session(event, "end", "Idle", running=False)
        send_compact_state(agent_name, "Idle", "")
        return 0

    if event_name == "UserPromptSubmit":
        prompt = truncate(str(event.get("prompt") or ""), 60) or "Thinking…"
        push_session(event, "update", prompt, detail=short_cwd, running=True)
        send_compact_state(agent_name, prompt, short_cwd)
        return 0

    if event_name == "PreToolUse":
        task, target, _ = describe_tool(tool_name, tool_input_for(event))
        display = f"{task}: {target}" if target else task
        push_session(event, "update", display, detail=target or short_cwd, running=True)
        send_compact_state(agent_name, display, short_cwd)
        if tool_name == "AskUserQuestion":
            return handle_ask_user_question(event, agent_name, short_cwd)
        if tool_name in WRITE_TOOLS:
            # Respect the session's permission mode. Hooks fire before
            # Claude Code's own permission check, so we must not re-prompt
            # what the user already chose to skip.
            if is_bypass_mode(event):
                return 0
            if is_edit_accepted_mode(event) and tool_name in EDIT_TOOLS:
                return 0
            return handle_pretool_approval(event, agent_name)
        return 0

    if event_name == "PermissionRequest":
        # PermissionRequest only fires when Claude Code is about to show its
        # own prompt. In bypass mode it shouldn't fire at all, but if it does,
        # honor the user's intent and pass through without blocking.
        if is_bypass_mode(event):
            permission_decision("allow", updated_input=tool_input_for(event))
            return 0
        return handle_permission_request(event, agent_name, short_cwd)

    if event_name == "PostToolUse":
        task, _, _ = describe_tool(tool_name, tool_input_for(event))
        display = f"Done: {task}"
        push_session(event, "update", display, detail=short_cwd, running=True)
        send_compact_state(agent_name, display, short_cwd)
        return 0

    if event_name == "PostToolUseFailure":
        display = f"Failed: {tool_name}" if tool_name else "Tool failed"
        push_session(event, "update", display, detail=short_cwd, running=True, event_kind="failed")
        send_compact_state(agent_name, display, short_cwd)
        return 0

    if event_name == "Stop":
        response_text = truncate(str(event.get("last_assistant_message") or event.get("responseText") or ""), 70)
        display = response_text or "Idle"
        push_session(event, "update", display, detail=short_cwd, running=False, event_kind="completed")
        send_compact_state(agent_name, display, short_cwd)
        return 0

    if event_name == "StopFailure":
        error = truncate(str(event.get("error") or event.get("responseText") or event.get("last_assistant_message") or ""), 80)
        display = error or "Session failure"
        push_session(event, "update", f"Failed: {display}", detail=short_cwd, running=False, event_kind="failed")
        send_compact_state(agent_name, f"Failed: {display}", short_cwd)
        return 0

    if event_name == "Notification":
        notification_type = str(event.get("notification_type") or event.get("notificationType") or "").strip()
        message = truncate(str(event.get("message") or event.get("text") or ""), 60) or "Waiting for input"
        if notification_type in {"", "permission_prompt", "idle_prompt", "elicitation_dialog"}:
            push_session(event, "update", message, detail=short_cwd, running=False, event_kind="waiting")
            send_compact_state(agent_name, message, short_cwd)
            return 0
        send_compact_state(agent_name, message, short_cwd)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
