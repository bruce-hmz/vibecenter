"""Pure-Python port of scan-agents.sh — discovers running agent sessions.

Reads the same transcript/rollout/session stores as the macOS shell
scanner (all paths are dot-directories under the user profile, so they
resolve identically on Windows). Process enumeration is best-effort and
platform-specific (PowerShell on Windows, ps elsewhere); session
discovery itself is file-based and needs no process access.

Every path/window can be overridden via ScanConfig for deterministic
tests, mirroring the VIBE_ISLAND_* env vars of scan-agents.sh.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentSession, first_line

ACTIVE_WINDOW_SECS = 300
RUNNING_SECS = 15


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class ScanConfig:
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    now_epoch: Optional[int] = None
    only_sources: Optional[List[str]] = None
    # Per-source roots. Defaults mirror scan-agents.sh ($HOME-based).
    claude_projects: Optional[str] = None
    zcode_rollout_dir: Optional[str] = None
    zcode_agents_dir: Optional[str] = None
    codex_sessions_dir: Optional[str] = None
    antigravity_dir: Optional[str] = None
    gemini_cli_tmp_dir: Optional[str] = None
    qwen_tmp_dir: Optional[str] = None
    kimi_sessions_dirs: Optional[List[str]] = None
    opencode_storage_dirs: Optional[List[str]] = None
    deepseek_sessions_dir: Optional[str] = None
    active_window: int = ACTIVE_WINDOW_SECS
    zcode_app_running: Optional[bool] = None
    # Optional test injection: {pid: (cwd, command, start_epoch)}.
    process_fixture: Optional[Dict[int, Tuple[str, str, int]]] = None

    @staticmethod
    def from_env(**overrides: Any) -> "ScanConfig":
        def opt(name: str, default: Optional[str] = None) -> Optional[str]:
            if name in overrides and overrides[name] is not None:
                return str(overrides[name])
            value = _env(name)
            return value if value else default

        home = opt("VIBE_ISLAND_HOME", os.path.expanduser("~"))  # type: ignore[assignment]
        only_raw = _env("VIBE_ISLAND_ONLY_SOURCES")
        kimi_roots = opt("VIBE_ISLAND_KIMI_SESSIONS_DIR")
        if kimi_roots:
            kimi_list = [part for part in kimi_roots.split(":") if part]
        else:
            kimi_list = [os.path.join(home, ".kimi", "sessions"),
                         os.path.join(home, ".kimi-code", "sessions")]
        local_data = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        opencode_list = [
            os.path.join(local_data, "opencode", "storage"),
            os.path.join(home, ".local", "share", "opencode", "storage"),
        ]
        window = int(_env("VIBE_ISLAND_ACTIVE_WINDOW_SECS", str(ACTIVE_WINDOW_SECS)))
        zcode_running_raw = _env("VIBE_ISLAND_ZCODE_APP_RUNNING").strip().lower()
        return ScanConfig(
            home=home,
            now_epoch=int(_env("VIBE_ISLAND_NOW_EPOCH")) if _env("VIBE_ISLAND_NOW_EPOCH") else None,
            only_sources=[s for s in only_raw.split(",") if s] if only_raw else None,
            claude_projects=opt("VIBE_ISLAND_CLAUDE_PROJECTS",
                                os.path.join(home, ".claude", "projects")),
            zcode_rollout_dir=opt("VIBE_ISLAND_ZCODE_ROLLOUT_DIR",
                                  os.path.join(home, ".zcode", "cli", "rollout")),
            zcode_agents_dir=opt("VIBE_ISLAND_ZCODE_AGENTS_DIR",
                                 os.path.join(home, ".zcode", "cli", "agents")),
            codex_sessions_dir=opt("VIBE_ISLAND_CODEX_SESSIONS_DIR",
                                   os.path.join(home, ".codex", "sessions")),
            antigravity_dir=opt("VIBE_ISLAND_ANTIGRAVITY_DIR",
                                os.path.join(home, ".gemini", "antigravity-cli")),
            gemini_cli_tmp_dir=opt("VIBE_ISLAND_GEMINI_CLI_TMP_DIR",
                                   os.path.join(home, ".gemini", "tmp")),
            qwen_tmp_dir=opt("VIBE_ISLAND_QWEN_TMP_DIR",
                             os.path.join(home, ".qwen", "tmp")),
            kimi_sessions_dirs=kimi_list,
            opencode_storage_dirs=opt_list("VIBE_ISLAND_OPENCODE_STORAGE_DIRS") or opencode_list,
            deepseek_sessions_dir=opt("VIBE_ISLAND_DEEPSEEK_SESSIONS_DIR",
                                      os.path.join(home, ".deepseek", "sessions")),
            active_window=window,
            zcode_app_running=(zcode_running_raw in ("1", "true", "yes")) if zcode_running_raw else None,
        )


def opt_list(name: str) -> Optional[List[str]]:  # noqa: N802 (used by ScanConfig)
    value = _env(name)
    return [part for part in value.split(":") if part] if value else None


def source_enabled(config: ScanConfig, source: str) -> bool:
    if not config.only_sources:
        return True
    return source in config.only_sources


def now_of(config: ScanConfig) -> int:
    return config.now_epoch if config.now_epoch is not None else int(time.time())


def _recent_files(directory: str, pattern: str, window: int, now: int,
                  root_glob: bool = False) -> List[str]:
    """Files under directory matching pattern, modified within window,
    newest first. pattern may span subdirectories when root_glob."""
    if not directory:
        return []
    full = os.path.join(os.path.expanduser(directory), "**" if root_glob else "", pattern)
    candidates: List[Tuple[int, str]] = []
    for path in glob.glob(full, recursive=root_glob):
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        if now - mtime < window:
            candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _is_running(path: str, now: int) -> bool:
    try:
        return now - int(os.path.getmtime(path)) < RUNNING_SECS
    except OSError:
        return False


def _short_name(path: str) -> str:
    return os.path.basename(path.rstrip("/\\")) if path else ""


# ── process helpers (best-effort, not required for discovery) ─────────

def list_processes(config: ScanConfig) -> List[Tuple[int, str]]:
    """(pid, command) pairs; fixture injection wins for tests."""
    if config.process_fixture is not None:
        return [(pid, info[1]) for pid, info in config.process_fixture.items()]
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine "
                 "| ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            procs = json.loads(out) if out.strip() else []
            if isinstance(procs, dict):
                procs = [procs]
            return [(int(p.get("ProcessId") or 0), str(p.get("CommandLine") or ""))
                    for p in procs if p.get("ProcessId")]
        out = subprocess.run(["ps", "-axo", "pid,command"],
                             capture_output=True, text=True, timeout=10).stdout
        result = []
        for line in out.splitlines()[1:]:
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                result.append((int(parts[0]), parts[1]))
        return result
    except Exception:
        return []


def process_pids_named(config: ScanConfig, name: str) -> List[int]:
    """Pids whose command references the binary `name` (direct or shim)."""
    pids = []
    for pid, command in list_processes(config):
        tokens = command.split()
        if any(tok == name or tok.endswith("/" + name) or tok.lower().endswith("\\" + name)
               for tok in tokens):
            pids.append(pid)
    return pids


def attach_single_pid(config: ScanConfig, name: str) -> Tuple[Optional[int], str]:
    pids = process_pids_named(config, name)
    if len(pids) == 1:
        return pids[0], ""
    return None, ""


# ── Claude Code ─────────────────────────────────────────
# ~/.claude/projects/<encoded-cwd>/<session>.jsonl, encoded = cwd with
# path separators replaced by '-'. File-based scan (no process cwd
# access needed): recently modified transcripts are active sessions.

def _claude_activity(path: str) -> Tuple[str, str, str]:
    title, preview, last_ts = "Claude Code", "", ""
    for record in _read_jsonl(path):
        ts = record.get("timestamp") or ""
        if ts:
            last_ts = ts
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        texts: List[str] = []
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        elif isinstance(content, str):
            texts = [content]
        for text in texts:
            if role == "user" and text and not text.startswith("<"):
                if title == "Claude Code":
                    title = first_line(text, 60)
            elif role == "assistant" and text:
                preview = first_line(text, 70)
    return title, preview, last_ts


def _decode_claude_project_dir(encoded: str) -> str:
    """Best-effort decode of the encoded project dir back to a path."""
    value = encoded.replace("-", os.sep)
    if os.name == "nt":
        # Windows encodes like 'C--Users-x-proj'; restore the drive colon.
        m = re.match(r"^([A-Za-z])" + re.escape(os.sep) + os.sep, value)
        if m:
            value = m.group(1) + ":" + value[len(m.group(1)) + 1:]
        return value
    if value.startswith(os.sep):
        return value
    return os.sep + value


def scan_claude(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "claude") or not config.claude_projects:
        return []
    now = now_of(config)
    sessions: List[AgentSession] = []
    seen = set()
    for path in _recent_files(config.claude_projects, "*.jsonl", config.active_window, now,
                              root_glob=True):
        session_id = os.path.splitext(os.path.basename(path))[0]
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        encoded = os.path.basename(os.path.dirname(path))
        cwd = _decode_claude_project_dir(encoded)
        title, preview, last_ts = _claude_activity(path)
        sessions.append(AgentSession(
            id=session_id, source="claude", task=title or "Claude Code",
            preview=preview, detail=_short_name(cwd), cwd=cwd,
            transcript_path=path, last_ts=last_ts, running=_is_running(path, now),
            pid=None,
        ))
    return sessions


# ── ZCode ───────────────────────────────────────────────
# ~/.zcode/cli/rollout/model-io-<sess>.jsonl (+ optional transcript in
# ~/.zcode/cli/agents/<sess>/**/transcript.jsonl for the title).

def _zcode_title_from_transcript(config: ScanConfig, sess_id: str) -> str:
    agents_dir = config.zcode_agents_dir or ""
    if not agents_dir or not os.path.isdir(agents_dir):
        return ""
    for path in glob.glob(os.path.join(agents_dir, sess_id, "**", "transcript.jsonl"),
                          recursive=True):
        for record in _read_jsonl(path):
            if record.get("type") == "turn_started":
                payload = record.get("payload") or {}
                text = str(payload.get("input") or "")
                if text and not text.startswith("{"):
                    return first_line(text, 60)
    return ""


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def scan_zcode(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "zcode") or not config.zcode_rollout_dir:
        return []
    running_app = config.zcode_app_running
    if running_app is None:
        running_app = os.path.isdir(config.zcode_rollout_dir)
    if not running_app:
        return []
    now = now_of(config)
    sessions: List[AgentSession] = []
    for path in _recent_files(config.zcode_rollout_dir, "model-io-sess_*.jsonl",
                              config.active_window, now):
        if "subagent" in os.path.basename(path):
            continue
        sess_id = os.path.basename(path)[len("model-io-"):-len(".jsonl")]
        if not sess_id:
            continue
        title = _zcode_title_from_transcript(config, sess_id)
        rollout_title, preview, last_ts = "", "", ""
        for record in _read_jsonl(path):
            ts = record.get("completedAt") or record.get("startedAt") or ""
            if ts:
                last_ts = ts
            request = record.get("request") or {}
            for message in request.get("messages") or []:
                if message.get("role") != "user":
                    continue
                text = _message_text(message).strip()
                if text and not text.startswith(("<", "[")) and not rollout_title:
                    rollout_title = first_line(text, 60)
            response = record.get("response") or {}
            text = str(response.get("text") or "")
            if text:
                preview = first_line(text, 70)
            tool_calls = response.get("toolCalls") or []
            if tool_calls and isinstance(tool_calls, list) and not preview:
                name = tool_calls[0].get("toolName") or tool_calls[0].get("name") or ""
                if name:
                    preview = f"tool: {name}"
        sessions.append(AgentSession(
            id=sess_id, source="zcode", task=title or rollout_title or "ZCode",
            preview=preview, detail="", terminal="zcode", last_ts=last_ts,
            running=_is_running(path, now), transcript_path=path,
        ))
    return sessions


# ── Codex ───────────────────────────────────────────────
# ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl with session_meta records.

def scan_codex(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "codex") or not config.codex_sessions_dir:
        return []
    now = now_of(config)
    sessions: List[AgentSession] = []
    seen: Dict[str, AgentSession] = {}
    for path in _recent_files(config.codex_sessions_dir, "rollout-*.jsonl",
                              max(config.active_window, 600), now, root_glob=True):
        meta: Optional[Dict[str, Any]] = None
        title, preview, last_ts = "", "", ""
        for record in _read_jsonl(path):
            ts = record.get("timestamp") or ""
            if ts:
                last_ts = ts
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "session_meta":
                meta = payload
                if not title:
                    title = str(payload.get("title") or payload.get("instructions") or "")[:60]
            elif kind == "event_msg":
                event = payload.get("type")
                if event == "user_message" and not title:
                    message = payload.get("message")
                    if isinstance(message, str):
                        title = first_line(message, 60)
                elif event == "agent_message":
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip():
                        preview = first_line(message, 70)
        if not meta:
            continue
        session_id = str(meta.get("id") or "")
        cwd = str(meta.get("cwd") or "")
        if not session_id or not cwd:
            continue
        origin = meta.get("source")
        if isinstance(origin, dict) and (origin.get("subagent")
                                         or origin.get("thread_spawn")
                                         or origin.get("other")):
            continue
        existing = seen.get(session_id)
        session = AgentSession(
            id=session_id, source="codex", task=title or "Codex",
            preview=preview, detail=_short_name(cwd), cwd=cwd,
            transcript_path=path, last_ts=last_ts,
            running=_is_running(path, now),
        )
        if existing is None or _is_running(path, now) or not existing.transcript_path:
            seen[session_id] = session
    sessions.extend(seen.values())
    return sessions


# ── Gemini / Antigravity CLI ────────────────────────────

def scan_antigravity(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "gemini") or not config.antigravity_dir:
        return []
    root = config.antigravity_dir
    log_dir = os.path.join(root, "log")
    db_file = os.path.join(root, "conversation_summaries.db")
    now = now_of(config)
    logs = _recent_files(log_dir, "cli-*.log", config.active_window, now)
    if not logs or not os.path.isfile(db_file):
        return []
    pid, _ = attach_single_pid(config, "agy-bin")
    if pid is None:
        pid, _ = attach_single_pid(config, "agy")
    if pid is None:
        pids = set(process_pids_named(config, "agy-bin")) | set(process_pids_named(config, "agy"))
        if len(pids) != 1:
            return []
        pid = next(iter(pids))
    log_file = logs[0]
    conversation_id, log_ts = "", ""
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.search(r"conversation ([a-f0-9-]+)", line)
                if match:
                    conversation_id = match.group(1)
                ts = re.match(r"[IWE](\d{4}) (\d{2}:\d{2}:\d{2})", line)
                if ts:
                    log_ts = f"{ts.group(1)} {ts.group(2)}"
    except OSError:
        return []
    title, preview = "Antigravity", ""
    if conversation_id:
        try:
            conn = sqlite3.connect(db_file)
            row = conn.execute(
                "SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,)).fetchone()
            if row:
                title = str(row[0] or row[1] or "Antigravity")[:60]
                preview = str(row[1] or "")[:70]
            conn.close()
        except sqlite3.Error:
            pass
    return [AgentSession(
        id=f"gemini-{pid}", source="gemini", task=title,
        preview=preview, detail="", cwd="", transcript_path=log_file,
        last_ts=log_ts, running=_is_running(log_file, now), pid=pid,
    )]


# ── Native Gemini CLI / Qwen Code ───────────────────────

def _chats_activity(path: str) -> Tuple[str, str, str]:
    title, preview, last_ts = "", "", ""

    def text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ""

    for record in _read_jsonl(path):
        kind = record.get("type") or ""
        ts = record.get("timestamp") or ""
        if ts:
            last_ts = ts
        if kind == "user":
            text = text_of(record.get("content"))
            if text and not title and not text.lstrip().startswith(("<", "[")):
                title = first_line(text, 60)
        elif kind in ("gemini", "model", "assistant"):
            text = text_of(record.get("content"))
            if text:
                preview = first_line(text, 70)
            else:
                calls = record.get("toolCalls") or []
                if isinstance(calls, list) and calls and isinstance(calls[0], dict):
                    call = calls[0]
                    name = call.get("name") or (call.get("function") or {}).get("name") or ""
                    if name:
                        preview = f"tool: {name}"
    return title, preview, last_ts


def _scan_chats_style(config: ScanConfig, source: str, tmp_dir: Optional[str],
                      proc_name: str, default_title: str) -> List[AgentSession]:
    if not tmp_dir:
        return []
    now = now_of(config)
    pid, _ = attach_single_pid(config, proc_name)
    sessions: List[AgentSession] = []
    for path in _recent_files(tmp_dir, "session-*.jsonl", config.active_window, now,
                              root_glob=True):
        if os.path.basename(os.path.dirname(path)) != "chats":
            continue
        title, preview, last_ts = _chats_activity(path)
        slug_dir = os.path.dirname(os.path.dirname(path))
        cwd = ""
        try:
            with open(os.path.join(slug_dir, ".project_root"), "r", encoding="utf-8") as handle:
                cwd = handle.read().strip()
        except OSError:
            pass
        session_id = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
                header = json.loads(first) if first.strip() else {}
                if isinstance(header, dict):
                    session_id = str(header.get("sessionId") or "")
        except (OSError, ValueError):
            pass
        if not session_id:
            session_id = os.path.splitext(os.path.basename(path))[0]
        sessions.append(AgentSession(
            id=session_id, source=source, task=title or default_title,
            preview=preview, detail=_short_name(cwd), cwd=cwd,
            transcript_path=path, last_ts=last_ts,
            running=_is_running(path, now), pid=pid,
        ))
    return sessions


def scan_gemini_cli(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "gemini"):
        return []
    return _scan_chats_style(config, "gemini", config.gemini_cli_tmp_dir,
                             "gemini", "Gemini CLI")


def scan_qwen(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "qwen"):
        return []
    return _scan_chats_style(config, "qwen", config.qwen_tmp_dir, "qwen", "Qwen Code")


# ── Kimi CLI ────────────────────────────────────────────

def _wire_activity(path: str) -> Tuple[str, str, str]:
    title, preview, last_ts = "", "", ""

    def walk_text(node: Any) -> str:
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            for key in ("text", "content", "input", "message"):
                value = node.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, (dict, list)):
                    got = walk_text(value)
                    if got:
                        return got
        if isinstance(node, list):
            for item in node:
                got = walk_text(item)
                if got:
                    return got
        return ""

    def role_of(node: Dict[str, Any]) -> str:
        for key in ("role", "type", "speaker"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value.lower()
        return ""

    for record in _read_jsonl(path):
        ts = record.get("timestamp") or record.get("time") or ""
        if isinstance(ts, str) and ts:
            last_ts = ts
        nested = record.get("record") if isinstance(record.get("record"), dict) else record
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        role = role_of(record) or role_of(nested) or role_of(message)
        if role in ("user", "human") and not title:
            text = walk_text(record)
            if text and not text.lstrip().startswith(("<", "[")):
                title = first_line(text, 60)
        elif role in ("assistant", "agent", "kimi", "ai", "model"):
            text = walk_text(record)
            if text:
                preview = first_line(text, 70)
    return title, preview, last_ts


def scan_kimi(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "kimi") or not config.kimi_sessions_dirs:
        return []
    now = now_of(config)
    sessions: List[AgentSession] = []
    seen = set()
    for root in config.kimi_sessions_dirs:
        for path in _recent_files(root, "wire.jsonl", config.active_window, now,
                                  root_glob=True):
            relative = os.path.relpath(path, root)
            parts = relative.split(os.sep)
            # ws/<session>/agents/<agent>/wire.jsonl → session is 4th from
            # the end; plain <group>/<session>/wire.jsonl → 2nd from end.
            if len(parts) >= 4 and parts[-3] == "agents":
                session_dir = parts[-4]
            else:
                session_dir = parts[-2]
            if not session_dir or session_dir in seen:
                continue
            seen.add(session_dir)
            title, preview, last_ts = _wire_activity(path)
            sessions.append(AgentSession(
                id=session_dir, source="kimi", task=title or "Kimi",
                preview=preview, transcript_path=path, last_ts=last_ts,
                running=_is_running(path, now),
            ))
    return sessions


# ── OpenCode ────────────────────────────────────────────

def _opencode_message_text(storage: str, session_id: str, message: Dict[str, Any]) -> str:
    parts = message.get("parts")
    if isinstance(parts, list):
        text = " ".join(str(p.get("text") or "") for p in parts
                        if isinstance(p, dict) and p.get("type") == "text").strip()
        if text:
            return text
    mid = str(message.get("id") or "")
    if not mid:
        return ""
    for part_root in (os.path.join(storage, "message_part", session_id, mid),
                      os.path.join(storage, "part", mid)):
        part_files = sorted(glob.glob(os.path.join(part_root, "*.json")),
                            key=os.path.getmtime, reverse=True)
        for part_path in part_files[:4]:
            part = _read_json(part_path)
            if not part or part.get("type") != "text":
                continue
            blob = part.get("text")
            if not blob and isinstance(part.get("data"), dict):
                blob = part["data"].get("text")
            if isinstance(blob, str) and blob.strip():
                return blob.strip()
    return ""


def _scan_opencode_storage(config: ScanConfig, storage: str) -> List[AgentSession]:
    now = now_of(config)
    sessions: List[AgentSession] = []
    session_dir = os.path.join(storage, "session")
    for path in _recent_files(session_dir, "*.json", config.active_window, now,
                              root_glob=True):
        data = _read_json(path)
        if not data or data.get("parentID"):
            continue
        session_id = str(data.get("id") or os.path.splitext(os.path.basename(path))[0])
        cwd = str(data.get("directory") or data.get("cwd") or "")
        times = data.get("time") if isinstance(data.get("time"), dict) else {}
        last_ts = str(times.get("updated") or times.get("end") or times.get("created") or "")
        running = _is_running(path, now)
        title = str(data.get("title") or "").strip()
        preview = ""
        message_files = []
        message_dir = os.path.join(storage, "message", session_id)
        for message_path in glob.glob(os.path.join(message_dir, "*.json")):
            try:
                message_files.append((int(os.path.getmtime(message_path)), message_path))
            except OSError:
                continue
        message_files.sort()
        loaded: List[Tuple[float, Dict[str, Any]]] = []
        for mtime, message_path in message_files:
            message = _read_json(message_path)
            if message is not None:
                loaded.append((mtime, message))
        if loaded:
            running = running or (now - loaded[-1][0]) < RUNNING_SECS
            if not title:
                for _, message in loaded:
                    if str(message.get("role") or "") == "user":
                        text = _opencode_message_text(storage, session_id, message)
                        if text and not text.lstrip().startswith(("<", "[")):
                            title = first_line(text, 60)
                            break
            for _, message in reversed(loaded):
                if str(message.get("role") or "") == "assistant":
                    text = _opencode_message_text(storage, session_id, message)
                    if text:
                        preview = first_line(text, 70)
                        break
        sessions.append(AgentSession(
            id=session_id, source="opencode", task=title or "OpenCode",
            preview=preview, detail=_short_name(cwd), cwd=cwd,
            transcript_path=path, last_ts=last_ts, running=running,
        ))
    return sessions


def scan_opencode(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "opencode") or not config.opencode_storage_dirs:
        return []
    pid, _ = attach_single_pid(config, "opencode")
    sessions: List[AgentSession] = []
    for storage in config.opencode_storage_dirs:
        sessions.extend(_scan_opencode_storage(config, storage))
    for session in sessions:
        session.pid = pid
    return sessions


# ── DeepSeek ────────────────────────────────────────────

def scan_deepseek(config: ScanConfig) -> List[AgentSession]:
    if not source_enabled(config, "deepseek") or not config.deepseek_sessions_dir:
        return []
    now = now_of(config)
    files = _recent_files(config.deepseek_sessions_dir, "*.json", config.active_window, now)
    if len(files) != 1:
        return []  # ambiguous — same rule as the shell scanner
    pid, _ = attach_single_pid(config, "deepseek")
    path = files[0]
    data = _read_json(path)
    if not data:
        return []
    title, preview = "DeepSeek", ""
    for message in data.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        content = str(content or "")
        if role == "user" and content and title == "DeepSeek":
            title = first_line(content, 60)
        elif role == "assistant" and content:
            preview = first_line(content, 70)
    metadata = data.get("metadata") or {}
    last_ts = str(metadata.get("updatedAt") or metadata.get("createdAt") or "")
    return [AgentSession(
        id=f"deepseek-{pid}" if pid else os.path.splitext(os.path.basename(path))[0],
        source="deepseek", task=title, preview=preview,
        transcript_path=path, last_ts=last_ts, running=_is_running(path, now),
        pid=pid,
    )]


SCANNERS = [
    scan_claude, scan_zcode, scan_codex, scan_antigravity, scan_gemini_cli,
    scan_qwen, scan_kimi, scan_opencode, scan_deepseek,
]


def scan_all(config: Optional[ScanConfig] = None) -> List[AgentSession]:
    """Run every enabled source scanner and return deduped sessions."""
    config = config or ScanConfig.from_env()
    sessions: List[AgentSession] = []
    seen = set()
    for scanner in SCANNERS:
        try:
            for session in scanner(config):
                key = (session.source, session.id)
                if key in seen:
                    continue
                seen.add(key)
                if not session.last_update:
                    session.last_update = now_of(config)
                sessions.append(session)
        except Exception as exc:  # one broken source must not kill the scan
            print(f"scanner {scanner.__name__} failed: {exc}", flush=True)
    sessions.sort(key=lambda s: s.last_update, reverse=True)
    return sessions


def read_preview(path: str, source: str) -> str:
    """Re-read the latest activity preview for a watched transcript.

    Used by the file-watcher path; returns "" when nothing parseable.
    """
    if source == "zcode":
        for record in reversed(_read_jsonl(path)):
            response = record.get("response") or {}
            text = str(response.get("text") or "")
            if text:
                return first_line(text, 70)
            calls = response.get("toolCalls") or []
            if calls and isinstance(calls, list):
                name = calls[0].get("toolName") or calls[0].get("name") or ""
                if name:
                    return f"tool: {name}"
        return ""
    if source in ("gemini", "qwen"):
        return _chats_activity(path)[1]
    if source == "codex":
        for record in reversed(_read_jsonl(path)):
            payload = record.get("payload") or {}
            if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    return first_line(message, 70)
        return ""
    if source == "claude":
        return _claude_activity(path)[1]
    return ""
