"""Claude Code hook registration — Python port of install-hook.sh.

Writes relay.py hook entries into ~/.claude/settings.json, preserving
non-relay entries and staying idempotent. Uses sys.executable so the
command works on Windows (python.exe) and macOS/Linux alike.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Dict, List, Tuple

HOOK_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Stop", "StopFailure", "Notification",
    "PermissionRequest", "SessionEnd",
]
PRETOOL_MATCHER = "Bash|Edit|Write|NotebookEdit|AskUserQuestion"


def settings_path() -> str:
    return os.environ.get("CLAUDE_SETTINGS") or os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json"
    )


def relay_path() -> str:
    """Stable path used in hook commands.

    Frozen builds ship a standalone VibeCenterRelay.exe next to the panel
    exe; it is staged into ~/.vibe-island/bin on launch. When present it
    is preferred (no Python installation required on the target).
    """
    from . import frozen as frozen_mod

    if frozen_mod.is_frozen():
        relay_py, relay_exe = frozen_mod.ensure_relay_runtime()
        if relay_exe:
            return relay_exe
        return relay_py
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "relay.py")


def relay_command() -> str:
    target = relay_path()
    if target.lower().endswith(".exe"):
        return f'"{target}"'
    quoted = target.replace('"', "")
    return f'"{sys.executable}" "{quoted}"'


def _looks_like_relay_command(command: str) -> bool:
    normalized = command.lower().replace("\\", "/").strip().strip('"')
    if not ("vibe-island" in normalized or "vibecenter" in normalized):
        return False
    return normalized.endswith("/relay.py") or normalized.endswith("vibecenterrelay.exe")


def _is_relay_entry(entry: dict, command: str) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        existing = str(hook.get("command", ""))
        if existing == command or _looks_like_relay_command(existing):
            return True
    return False


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def install() -> Tuple[bool, str]:
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = _load(path) if os.path.exists(path) else {}
    if os.path.exists(path):
        backups = [p for p in os.listdir(os.path.dirname(path))
                   if p.startswith("settings.json.bak-pre-relay.")]
        if not backups:
            stamp = str(int(__import__("time").time()))
            shutil.copy2(path, f"{path}.bak-pre-relay.{stamp}")
    command = relay_command()
    hooks = cfg.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        entries = [e for e in (hooks.get(event) or []) if isinstance(e, dict)]
        cleaned = [e for e in entries if not _is_relay_entry(e, command)]
        entry = {"hooks": [{"type": "command", "command": command}]}
        if event == "PreToolUse":
            entry["matcher"] = PRETOOL_MATCHER
        cleaned.append(entry)
        hooks[event] = cleaned
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return True, f"已为 {len(HOOK_EVENTS)} 个事件安装 relay hook"


def uninstall() -> Tuple[bool, str]:
    path = settings_path()
    if not os.path.exists(path):
        return True, "未找到 settings.json"
    cfg = _load(path)
    command = relay_command()
    hooks = cfg.get("hooks") or {}
    for event in list(hooks.keys()):
        entries = [e for e in (hooks.get(event) or []) if isinstance(e, dict)]
        cleaned = [e for e in entries if not _is_relay_entry(e, command)]
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks", None)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return True, "已移除 relay hooks"


def coverage() -> Tuple[int, int, bool]:
    """(configured relay events, required, authenticated) for health UI."""
    cfg = _load(settings_path())
    hooks = cfg.get("hooks") or {}
    command = relay_command()
    configured = 0
    authenticated = False
    for event in HOOK_EVENTS:
        found = any(isinstance(e, dict) and _is_relay_entry(e, command)
                    for e in (hooks.get(event) or []))
        if found:
            configured += 1
            # relay.py itself performs authenticated IPC; a registered relay
            # command implies signed traffic.
            authenticated = True
    return configured, len(HOOK_EVENTS), authenticated


def missing_events() -> List[str]:
    cfg = _load(settings_path())
    hooks = cfg.get("hooks") or {}
    command = relay_command()
    missing = []
    for event in HOOK_EVENTS:
        found = any(isinstance(e, dict) and _is_relay_entry(e, command)
                    for e in (hooks.get(event) or []))
        if not found:
            missing.append(event)
    return missing
