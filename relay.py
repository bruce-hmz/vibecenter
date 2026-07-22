#!/usr/bin/env python3
"""
vibe-island-relay — Claude Code hook → self-made Vibe Island notch app.

Bridge between Claude Code's hook system (JSON on stdin) and the self-made
notch app's TCP IPC protocol on 127.0.0.1:14321.

Routing:
  UserPromptSubmit  → compact (show prompt snippet, non-blocking)
  PreToolUse (write tools: Bash|Edit|Write|NotebookEdit) → approval (BLOCK
    until user clicks Allow/Deny; exit 0 = allow, exit 2 = deny)
  PreToolUse (read-only tools) → compact (non-blocking, just status update)
  PostToolUse      → compact (show what just finished)
  Stop             → compact "Idle" (non-blocking)
  SessionStart     → compact "Session start"
  Notification     → compact (waiting-for-input / idle hint)
  SessionEnd       → compact "Idle" (dismiss any pending request)

If the notch app isn't running, every event degrades to a no-op exit 0 so
Claude Code is never blocked by a missing UI.
"""
import json
import os
import socket
import sys

HOST, PORT = "127.0.0.1", 14321
TIMEOUT = 600  # match Claude Code's PreToolUse default

# Tools that mutate state and therefore require explicit user approval.
WRITE_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}


def send(payload, wait_response=False):
    """Send one JSON line to the notch app.

    Returns the parsed JSON response when wait_response=True and the app
    replied within TIMEOUT, otherwise None.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((HOST, PORT))
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            if not wait_response:
                return None
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8").strip())
    except (ConnectionRefusedError, socket.timeout, OSError):
        return None


def truncate(s, n=80):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def main():
    try:
        raw = sys.stdin.read()
        evt = json.loads(raw) if raw else {}
    except Exception:
        evt = {}

    name = evt.get("hook_event_name", "")
    tool = evt.get("tool_name", "")
    tool_input = evt.get("tool_input", {}) or {}
    cwd = evt.get("cwd", "") or os.getcwd()
    session = evt.get("session_id", "")

    # Short folder name for display: last path segment.
    short_cwd = os.path.basename(cwd.rstrip("/")) or cwd

    # Infer agent source. Claude Code sets CLAUDE_PRODUCT_NAME; ZCode sets
    # ZCODE_* or runs through a different binary. Fall back to "claude"
    # since that's the primary hook source for this relay.
    source = "claude"
    if os.environ.get("ZCODE_SESSION_ID") or os.environ.get("ZCODE_CLI"):
        source = "zcode"
    elif "zcode" in os.environ.get("TERM_PROGRAM", "").lower():
        source = "zcode"

    def push_session(action, task="", detail=None, running=None):
        """Tell the notch app about session lifecycle."""
        if not session:
            return
        payload = {"session": action, "session_id": session,
                   "source": source, "task": task}
        if detail:
            payload["detail"] = detail
        if running is not None:
            payload["running"] = running
        send(payload)

    if name == "UserPromptSubmit":
        prompt = truncate(evt.get("prompt", ""), 60)
        push_session("update", task=prompt or "Thinking…", running=True)
        send({
            "state": "compact",
            "agent": "Claude Code",
            "task": prompt or "Thinking…",
            "targetFile": short_cwd,
        })
        sys.exit(0)

    if name == "PreToolUse":
        if tool in WRITE_TOOLS:
            # Build a short target description for the approval card.
            if tool == "Bash":
                target = truncate(tool_input.get("command", ""), 70)
                task = "Run command"
            elif tool in ("Edit", "Write"):
                target = tool_input.get("file_path", "")
                task = "Edit file" if tool == "Edit" else "Write file"
            elif tool == "NotebookEdit":
                target = tool_input.get("notebook_path", "")
                task = "Edit notebook"
            else:
                target = ""
                task = "Modify"
            target = truncate(target, 70)

            resp = send({
                "state": "approval",
                "agent": "Claude Code",
                "task": task,
                "targetFile": target,
            }, wait_response=True)
            if not resp:
                # App not running → don't block the agent.
                sys.exit(0)
            action = str(resp.get("action", "")).lower()
            if action == "allow":
                push_session("update", task=f"{task}: {target}", detail=target, running=True)
                send({"state": "compact", "agent": "Claude Code",
                      "task": f"{task}: {target}", "targetFile": short_cwd})
                sys.exit(0)
            else:
                # Deny → exit 2 feeds stderr back to Claude as feedback.
                sys.stderr.write(f"User denied {task}: {target}\n")
                sys.exit(2)
        else:
            # Read-only tool: just update compact status.
            send({"state": "compact", "agent": "Claude Code",
                  "task": f"Reading {tool}", "targetFile": short_cwd})
            sys.exit(0)

    if name == "PostToolUse":
        push_session("update", task=f"Done: {tool}", running=True)
        send({"state": "compact", "agent": "Claude Code",
              "task": f"Done: {tool}", "targetFile": short_cwd})
        sys.exit(0)

    if name == "Stop":
        push_session("update", task="Idle", running=False)
        send({"state": "compact", "agent": "Claude Code",
              "task": "Idle", "targetFile": short_cwd})
        sys.exit(0)

    if name == "SessionStart":
        push_session("start", task="Session start", detail=short_cwd)
        send({"state": "compact", "agent": "Claude Code",
              "task": "Session start", "targetFile": short_cwd})
        sys.exit(0)

    if name == "Notification":
        # Claude Code sends this when it needs user attention (waiting for
        # input, idle timeout, permission request). Show a compact hint.
        msg = truncate(evt.get("message", ""), 50) or "Waiting for input"
        send({"state": "compact", "agent": "Claude Code",
              "task": msg, "targetFile": short_cwd})
        sys.exit(0)

    if name == "SessionEnd":
        push_session("end", running=False)
        send({"state": "compact", "agent": "Claude Code",
              "task": "Idle", "targetFile": ""})
        sys.exit(0)

    # Unknown event: don't interfere.
    sys.exit(0)


if __name__ == "__main__":
    main()
