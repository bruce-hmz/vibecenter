"""Data models shared by the scanner, IPC server, and UI.

Mirrors the AgentSession / PendingRequest / ApprovalRisk types in
VibeIsland.swift so scan output and hook payloads look identical on both
platforms.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SOURCE_COLORS = {
    "claude": "#D68C59",     # warm orange
    "zcode": "#60A5FA",      # blue
    "codex": "#22D3EE",      # cyan
    "gemini": "#8B5CF6",     # purple
    "deepseek": "#60A5FA",   # blue
    "kimi": "#EC4899",       # pink
    "grok": "#FFFFFF",       # white
    "qwen": "#7A6EF5",       # indigo
    "opencode": "#F59E0B",   # amber
}

RISK_COLORS = {
    "low": "#34D399",
    "medium": "#FBBF24",
    "high": "#F97316",
    "critical": "#EF4444",
}

RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "critical": "严重风险",
}

RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def source_color(source: str) -> str:
    return SOURCE_COLORS.get(source.strip().lower(), "#9CA3AF")


def provider_label(source: str) -> str:
    value = source.strip().lower()
    if value == "zcode":
        return "ZCODE"
    if value == "deepseek":
        return "DEEPSEEK"
    return value.upper() or "AGENT"


def first_line(text: str, limit: int) -> str:
    line = (text or "").strip().split("\n")[0]
    return line[:limit]


def parse_iso_ts(value: str) -> Optional[float]:
    """Parse common ISO-8601 shapes into an epoch float, or None."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        import datetime

        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except Exception:
        return None


@dataclass
class AgentSession:
    """One live agent conversation tracked in the notch."""

    id: str
    source: str = "claude"
    task: str = "Session"
    preview: str = ""
    detail: str = ""
    cwd: str = ""
    transcript_path: str = ""
    terminal: str = ""
    last_ts: str = ""
    last_update: float = 0.0
    running: bool = False
    scan_managed: bool = True
    has_live_updates: bool = False
    ended_at: Optional[float] = None
    pid: Optional[int] = None

    @property
    def color(self) -> str:
        return source_color(self.source)

    @property
    def provider(self) -> str:
        return provider_label(self.source)

    @property
    def workspace_name(self) -> str:
        if self.cwd:
            return self.cwd.rstrip("/\\").split("/")[-1].split("\\")[-1]
        if self.detail:
            return self.detail.rstrip("/\\").split("/")[-1].split("\\")[-1]
        return ""

    def relative_time(self, now: Optional[float] = None) -> str:
        seconds = int((now or time.time()) - (self.last_update or time.time()))
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return "now"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def ended_relative_time(self, now: Optional[float] = None) -> str:
        if self.ended_at is None:
            return ""
        seconds = int((now or time.time()) - self.ended_at)
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60}m前"
        if seconds < 86400:
            return f"{seconds // 3600}h前"
        return f"{seconds // 86400}d前"

    def to_scan_json(self) -> Dict[str, Any]:
        """Shape emitted by scan-agents.sh (used by tests for parity)."""
        return {
            "session": "start",
            "session_id": self.id,
            "source": self.source,
            "task": self.task,
            "detail": self.detail,
            "preview": self.preview,
            "terminal": self.terminal,
            "last_ts": self.last_ts,
            "running": self.running,
            "display_detail": self.detail,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "match_confidence": "recent_scan",
            "pid": str(self.pid or ""),
        }


@dataclass
class AskOption:
    id: str
    label: str
    description: str = ""


@dataclass
class AskQuestion:
    id: str
    header: str
    question: str
    multi_select: bool = False
    options: List[AskOption] = field(default_factory=list)


@dataclass
class PendingRequest:
    """An approval or AskUserQuestion card waiting for a decision."""

    id: str
    kind: str  # "approval" | "ask"
    session_id: str = ""
    source: str = "claude"
    agent_name: str = "Claude Code"
    task_name: str = "Permission request"
    target_file: str = ""
    tool_name: str = ""
    command: str = ""
    cwd: str = ""
    reason: str = ""
    diff: str = ""
    questions: List[AskQuestion] = field(default_factory=list)
    arrived_at: float = 0.0
    expires_at: float = 0.0

    @property
    def risk(self) -> "RiskAssessment":
        return assess_risk(self)

    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))


@dataclass
class RiskAssessment:
    level: str = "low"
    reasons: List[str] = field(default_factory=list)


def _contains(value: str, pattern: str) -> bool:
    return re.search(pattern, value) is not None


def _is_write_tool(tool: str) -> bool:
    return tool in ("edit", "write", "notebookedit")


def _target_outside_workspace(target: str, cwd: str) -> bool:
    """Port of ApprovalRiskAnalyzer.targetIsOutsideWorkspace."""
    if not target or not cwd:
        return False
    workspace = os.path.realpath(os.path.expanduser(cwd))
    expanded = os.path.expanduser(target)
    resolved = expanded if os.path.isabs(expanded) else os.path.join(workspace, expanded)
    resolved = os.path.realpath(resolved)
    return resolved != workspace and not resolved.startswith(
        workspace.rstrip("/\\") + os.sep
    )


def assess_risk(request: PendingRequest) -> RiskAssessment:
    """Port of the Swift ApprovalRiskAnalyzer (same rules and reasons)."""
    if request.kind != "approval":
        return RiskAssessment("low", ["不执行工具操作"])

    tool = (request.tool_name or "").lower()
    command = (request.command or "").lower()
    level = "low"
    reasons: List[str] = []

    def raise_risk(candidate: str, reason: str) -> None:
        nonlocal level
        if RISK_RANK[candidate] > RISK_RANK[level]:
            level = candidate
        if reason not in reasons:
            reasons.append(reason)

    if _contains(command, r"\b(git\s+reset\s+--hard|git\s+clean\s+-[^\s]*f[^\s]*d|git\s+clean\s+-[^\s]*d[^\s]*f)\b"):
        raise_risk("critical", "可能不可逆地丢弃版本控制内容")
    if _contains(command, r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b"):
        raise_risk("critical", "下载内容将直接交给 Shell 执行")
    if _contains(command, r"\brm\s+-[^\s]*r[^\s]*f[^\s]*\s+(/|~|\$home)(\s|$)") or _contains(
        command, r"\brm\s+-[^\s]*f[^\s]*r[^\s]*\s+(/|~|\$home)(\s|$)"
    ):
        raise_risk("critical", "可能删除系统或用户目录")
    if "diskutil erase" in command or "mkfs" in command or _contains(command, r"\bdd\b[^\n]*\bof=/dev/"):
        raise_risk("critical", "可能覆盖磁盘或设备数据")

    if _contains(command, r"\brm\s+-[^\s]*(r[^\s]*f|f[^\s]*r)"):
        raise_risk("high", "包含递归强制删除")
    if _contains(command, r"(^|[;&|]\s*)sudo\s+"):
        raise_risk("high", "需要管理员权限")
    if _contains(command, r"\bgit\s+push\b"):
        raise_risk("high", "会写入远端仓库")
    if _contains(command, r"\b(chmod|chown)\s+-[^\s]*r") or "launchctl " in command or "killall " in command:
        raise_risk("high", "会批量修改系统状态")

    if _is_write_tool(tool) and _target_outside_workspace(request.target_file, request.cwd):
        raise_risk("high", "写入位置在当前工作区之外")
    if tool in ("bash", "shell") or command:
        raise_risk("medium", "将执行 Shell 命令")
    elif _is_write_tool(tool) or request.diff:
        raise_risk("medium", "将修改文件内容")
    else:
        raise_risk("medium", "工具会产生本机副作用")

    return RiskAssessment(level, reasons)


HISTORY_PROVIDERS = {"claude", "codex", "zcode", "gemini", "deepseek", "kimi", "grok"}


@dataclass
class DecisionHistoryEntry:
    """Privacy-preserving approval decision record (no content)."""

    id: str
    decided_at: float
    provider: str
    tool_category: str
    risk: str
    outcome: str
    decision_source: str

    @staticmethod
    def sanitized(request: PendingRequest, action: str, decision_source: str) -> "DecisionHistoryEntry":
        raw_source = request.source.lower()
        provider = raw_source if raw_source in HISTORY_PROVIDERS else "other"
        tool = (request.tool_name or "").lower()
        if tool in ("bash", "shell"):
            category = "shell"
        elif tool in ("edit", "write"):
            category = "file"
        elif tool == "notebookedit":
            category = "notebook"
        else:
            category = "other"
        outcome = "allow" if (action or "").lower() == "allow" else "deny"
        allowed_sources = {"approval_button", "queue_button", "queue_allow_all", "timeout"}
        source = decision_source if decision_source in allowed_sources else "other"
        return DecisionHistoryEntry(
            id=uuid.uuid4().hex,
            decided_at=time.time(),
            provider=provider,
            tool_category=category,
            risk=request.risk.level,
            outcome=outcome,
            decision_source=source,
        )

    def summary(self) -> str:
        tool_label = {
            "shell": "Shell",
            "file": "文件修改",
            "notebook": "Notebook",
        }.get(self.tool_category, "工具")
        provider = self.provider.upper() if self.provider != "other" else "AGENT"
        return f"{provider} · {tool_label}"

    def outcome_label(self) -> str:
        return "已允许" if self.outcome == "allow" else "已拒绝"

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "decidedAt": self.decided_at,
            "provider": self.provider,
            "toolCategory": self.tool_category,
            "risk": self.risk,
            "outcome": self.outcome,
            "decisionSource": self.decision_source,
        }

    @staticmethod
    def from_json(data: Dict[str, Any]) -> "DecisionHistoryEntry":
        return DecisionHistoryEntry(
            id=str(data.get("id") or uuid.uuid4().hex),
            decided_at=float(data.get("decidedAt") or 0.0),
            provider=str(data.get("provider") or "other"),
            tool_category=str(data.get("toolCategory") or "other"),
            risk=str(data.get("risk") or "low"),
            outcome=str(data.get("outcome") or "deny"),
            decision_source=str(data.get("decisionSource") or "other"),
        )


@dataclass
class UsageSnapshot:
    """One provider's usage snapshot pushed by usage-daemon.py."""

    provider: str = "Z.ai"
    five_hour: Optional[int] = None
    five_hour_reset: str = ""
    seven_day: Optional[int] = None
    seven_day_reset: str = ""
    monthly: Optional[int] = None
    monthly_reset: str = ""
    level: str = ""
    plan: str = ""

    @staticmethod
    def from_payload(provider: str, usage: Dict[str, Any]) -> "UsageSnapshot":
        def opt_int(value: Any) -> Optional[int]:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return UsageSnapshot(
            provider=provider or "Z.ai",
            five_hour=opt_int(usage.get("five_hour")),
            five_hour_reset=str(usage.get("five_hour_reset") or ""),
            seven_day=opt_int(usage.get("seven_day")),
            seven_day_reset=str(usage.get("seven_day_reset") or ""),
            monthly=opt_int(usage.get("monthly")),
            monthly_reset=str(usage.get("monthly_reset") or ""),
            level=str(usage.get("level") or ""),
            plan=str(usage.get("plan") or ""),
        )

    def summary_line(self) -> str:
        parts = []
        if self.five_hour is not None:
            parts.append(f"5h {self.five_hour}" + (f"（{self.five_hour_reset}）" if self.five_hour_reset else ""))
        if self.seven_day is not None:
            parts.append(f"7d {self.seven_day}")
        if self.monthly is not None:
            parts.append(f"月 {self.monthly}")
        return " · ".join(parts) or "暂无数据"


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
