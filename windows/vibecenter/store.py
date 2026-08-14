"""Qt-facing application state — sessions, requests, usage, settings.

IPC callbacks arrive on server threads; they funnel through IPCBridge
signals (auto queued connections) so all mutation happens on the GUI
thread. The API mirrors the Swift NotchViewModel semantics: upsert,
reconcile scan-managed sessions, queue timeout, history cap of 30.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from . import models
from .models import (
    AgentSession,
    DecisionHistoryEntry,
    PendingRequest,
    UsageSnapshot,
)

HISTORY_LIMIT = 30
REQUEST_TIMEOUT_SECONDS = 300

SUPPORTED_PROVIDERS = ("claude", "codex", "zcode", "gemini", "deepseek", "kimi", "grok")


def state_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".vibe-island")
    os.makedirs(path, exist_ok=True)
    return path


def settings_path() -> str:
    return os.path.join(state_dir(), "settings.json")


def history_path() -> str:
    return os.path.join(state_dir(), "approval-history.json")


class IPCBridge(QObject):
    """Signal bridge from IPC server threads into the GUI thread."""

    sessionMessage = Signal(dict)
    compactMessage = Signal(dict)
    usageMessage = Signal(dict)
    usageStatusMessage = Signal(dict)
    requestEnqueued = Signal(object, object)  # PendingRequest, HeldRequest
    clientClosed = Signal(str)


class Store(QObject):
    sessionsChanged = Signal()
    requestsChanged = Signal()
    usageChanged = Signal()
    healthChanged = Signal()
    sessionEvent = Signal(str, str)  # kind, session_id
    notifyRequest = Signal(object)   # PendingRequest
    pinRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.sessions: Dict[str, AgentSession] = {}
        self.history_sessions: List[AgentSession] = []
        self.requests: List[PendingRequest] = []
        self.usage_providers: List[UsageSnapshot] = []
        self.usage_index = 0
        self.compact_task = ""
        self.compact_agent = ""
        # health: ipc / scanner / usage / hook → (kind, detail)
        self.health: Dict[str, tuple] = {
            "ipc": ("checking", "正在启动 IPC 服务"),
            "scanner": ("checking", "正在扫描 Agent"),
            "usage": ("disabled", "用量监测已关闭"),
            "hook": ("checking", "正在检查 Claude Code Hook"),
        }
        self.settings: Dict[str, Any] = models.load_json(settings_path(), {}) or {}
        self.history: List[DecisionHistoryEntry] = self._load_history()
        self._decide_callbacks: Dict[str, Any] = {}  # request_id → HeldRequest

    # ── settings ──────────────────────────────────────────

    def _persist_settings(self) -> None:
        models.save_json(settings_path(), self.settings)

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.settings.get("notifications", True))

    def set_notifications_enabled(self, value: bool) -> None:
        self.settings["notifications"] = bool(value)
        self._persist_settings()

    @property
    def auto_start_usage(self) -> bool:
        return bool(self.settings.get("autoStartUsage", True))

    def set_auto_start_usage(self, value: bool) -> None:
        self.settings["autoStartUsage"] = bool(value)
        self._persist_settings()

    @property
    def history_enabled(self) -> bool:
        return bool(self.settings.get("decisionHistory", True))

    def set_history_enabled(self, value: bool) -> None:
        self.settings["decisionHistory"] = bool(value)
        if not value:
            self.history = []
            self._persist_history()
        else:
            self._persist_settings()

    # ── sessions ──────────────────────────────────────────

    def upsert_session(self, session: AgentSession, live: bool = False) -> None:
        existing = self.sessions.get(session.id)
        if existing:
            session.has_live_updates = existing.has_live_updates or live
            if live:
                # Live hook updates win over scan snapshots except the
                # richer scan-only context fields.
                session.scan_managed = existing.scan_managed
        if live:
            session.has_live_updates = True
        if not session.last_update:
            session.last_update = time.time()
        self.sessions[session.id] = session
        self._prune_history_dupes(session.id)
        self.sessionsChanged.emit()

    def _prune_history_dupes(self, session_id: str) -> None:
        self.history_sessions = [s for s in self.history_sessions if s.id != session_id]

    def apply_session_message(self, message: Dict[str, Any]) -> None:
        action = str(message.get("session") or "")
        session_id = str(message.get("session_id") or "")
        if not session_id:
            return
        source = str(message.get("source") or "claude")
        if action == "end":
            self.remove_session(session_id)
            return
        ts = models.parse_iso_ts(str(message.get("last_ts") or "")) or time.time()
        self.upsert_session(AgentSession(
            id=session_id,
            source=source,
            task=str(message.get("task") or "Session"),
            preview=str(message.get("preview") or ""),
            detail=str(message.get("detail") or ""),
            cwd=str(message.get("cwd") or ""),
            running=bool(message.get("running")),
            last_update=ts,
            scan_managed=False,
            has_live_updates=True,
        ), live=True)
        event_kind = str(message.get("event_kind") or "")
        if event_kind in ("completed", "failed", "waiting"):
            self.sessionEvent.emit(event_kind, session_id)

    def remove_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.ended_at = time.time()
            self.history_sessions.insert(0, session)
            del self.history_sessions[12:]
        for request in [r for r in self.requests if r.session_id == session_id]:
            self.requests.remove(request)
        self.sessionsChanged.emit()
        self.requestsChanged.emit()

    def reconcile_scanned(self, scanned: List[AgentSession]) -> List[str]:
        """Adopt scan results; drop scan-managed sessions the scan lost.

        Live hook-registered sessions are never removed by reconciliation.
        Returns removed ids so the controller can drop file watchers.
        """
        scanned_ids = {s.id for s in scanned}
        removed = []
        for session in scanned:
            existing = self.sessions.get(session.id)
            if existing is not None and existing.has_live_updates:
                # Keep live state; refresh only scan-side context.
                existing.cwd = session.cwd or existing.cwd
                existing.transcript_path = session.transcript_path or existing.transcript_path
                existing.terminal = session.terminal or existing.terminal
                if session.pid:
                    existing.pid = session.pid
                continue
            session.scan_managed = True
            self.sessions[session.id] = session
        for session_id, session in list(self.sessions.items()):
            if session.scan_managed and session_id not in scanned_ids:
                del self.sessions[session_id]
                removed.append(session_id)
        self.sessionsChanged.emit()
        return removed

    # ── pending requests ──────────────────────────────────

    @property
    def current_request(self) -> Optional[PendingRequest]:
        return self.requests[0] if self.requests else None

    def enqueue_request(self, request: PendingRequest, held) -> None:
        if any(r.id == request.id for r in self.requests):
            return  # duplicate push (PreToolUse + PermissionRequest dedup upstream)
        self.requests.append(request)
        self._decide_callbacks[request.id] = held
        self.requests.sort(key=lambda r: r.arrived_at)
        self.requestsChanged.emit()
        self.notifyRequest.emit(request)
        self.pinRequested.emit()

    def decide(self, request_id: str, action: str, answers: Optional[Dict[str, Any]] = None,
               decision_source: str = "approval_button") -> None:
        request = next((r for r in self.requests if r.id == request_id), None)
        held = self._decide_callbacks.pop(request_id, None)
        self.requests = [r for r in self.requests if r.id != request_id]
        self.requestsChanged.emit()
        response: Dict[str, Any] = {"request_id": request_id}
        if action == "cancel":
            response["action"] = "cancel"
            response["reason"] = "user_cancelled"
        elif request is not None and request.kind == "ask":
            response["answers"] = answers or {}
        else:
            response["action"] = "allow" if action == "allow" else "deny"
        if held is not None:
            held.decide(response)
        if request is not None and request.kind == "approval" and action != "cancel":
            self.record_decision(request, response.get("action", "deny"), decision_source)

    def expire_request(self, request_id: str) -> None:
        """Called by the countdown timer — fail-closed deny."""
        request = next((r for r in self.requests if r.id == request_id), None)
        if request is None:
            return
        self.decide(request_id, "deny", decision_source="timeout")

    def allow_all_pending_safe(self) -> int:
        """Approve every low/medium-risk request; risky ones stay."""
        allowed = 0
        for request in list(self.requests):
            if request.kind == "approval" and request.risk.level in ("low", "medium"):
                self.decide(request.id, "allow", decision_source="queue_allow_all")
                allowed += 1
        return allowed

    def dismiss_stale_requests(self) -> None:
        now = time.time()
        for request in list(self.requests):
            if request.expires_at <= now:
                self.decide(request.id, "deny", decision_source="timeout")

    # ── decision history (privacy-preserving) ─────────────

    def _load_history(self) -> List[DecisionHistoryEntry]:
        if not self.history_enabled:
            return []
        raw = models.load_json(history_path(), []) or []
        entries = [DecisionHistoryEntry.from_json(item) for item in raw
                   if isinstance(item, dict)]
        return entries[:HISTORY_LIMIT]

    def _persist_history(self) -> None:
        models.save_json(history_path(),
                         [entry.to_json() for entry in self.history[:HISTORY_LIMIT]])

    def record_decision(self, request: PendingRequest, action: str,
                        decision_source: str) -> None:
        if not self.history_enabled:
            return
        self.history.insert(0, DecisionHistoryEntry.sanitized(request, action, decision_source))
        del self.history[HISTORY_LIMIT:]
        self._persist_history()

    # ── usage ─────────────────────────────────────────────

    def apply_usage(self, message: Dict[str, Any]) -> None:
        usage = message.get("usage") or {}
        provider = str(usage.get("provider") or message.get("provider") or "Z.ai")
        snapshot = UsageSnapshot.from_payload(provider, usage)
        for index, existing in enumerate(self.usage_providers):
            if existing.provider == provider:
                self.usage_providers[index] = snapshot
                break
        else:
            self.usage_providers.append(snapshot)
        if self.usage_index >= len(self.usage_providers):
            self.usage_index = 0
        self.set_health("usage", "ready", "配额数据已更新")
        self.usageChanged.emit()

    def advance_usage(self) -> None:
        if len(self.usage_providers) > 1:
            self.usage_index = (self.usage_index + 1) % len(self.usage_providers)
            self.usageChanged.emit()

    @property
    def current_usage(self) -> Optional[UsageSnapshot]:
        if not self.usage_providers:
            return None
        return self.usage_providers[min(self.usage_index, len(self.usage_providers) - 1)]

    # ── health ────────────────────────────────────────────

    def set_health(self, key: str, kind: str, detail: str) -> None:
        if self.health.get(key) != (kind, detail):
            self.health[key] = (kind, detail)
            self.healthChanged.emit()
