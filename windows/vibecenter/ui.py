"""Notch panel UI — PySide6 port of the macOS notch overlay.

A frameless, always-on-top window pinned to the top-center of the
primary screen: compact pill by default, expands on hover, pins on
click, and force-expands with an approval/ask card when an agent needs
a decision. Dark theme, DepartureMono when available.

Windows has no physical notch; the panel renders the same silhouette
(square top corners, rounded bottom) hugging the top edge.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter,
                           QPainterPath, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame,
                               QHBoxLayout, QLabel, QMenu, QMessageBox,
                               QPushButton, QRadioButton, QScrollArea,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from .models import RISK_COLORS, RISK_LABELS, source_color
from .store import Store

BG = QColor(16, 16, 18)
BG_DARKER = QColor(10, 10, 12)
BORDER = QColor(56, 56, 62)
TEXT = QColor(235, 235, 240)
TEXT_MUTED = QColor(140, 140, 150)
GREEN = QColor(52, 211, 153)

COMPACT_W, COMPACT_H = 340, 36
EXPANDED_W = 460
PULSE_FRAMES = ["▁▂▃", "▂▃▄", "▃▄▅", "▄▅▆", "▅▆▇", "▆▇█", "▇█▇", "█▇▆"]


def load_app_font() -> Optional[str]:
    """Register DepartureMono; return the family or None.

    Looks in the repo root, next to the executable (frozen onedir), and
    the PyInstaller bundle dir (frozen onefile).
    """
    from . import frozen as frozen_mod

    candidates = [
        os.path.join(frozen_mod.REPO_ROOT, "DepartureMono-Regular.otf"),
        os.path.join(frozen_mod.exe_dir(), "DepartureMono-Regular.otf"),
        frozen_mod.bundled_path("DepartureMono-Regular.otf"),
    ]
    for path in candidates:
        if os.path.exists(path):
            font_id = QFontDatabase.addApplicationFont(path)
            if font_id >= 0:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    return families[0]
    return None


def mono(family: Optional[str], point_size: int = 10, bold: bool = False) -> QFont:
    if family:
        font = QFont(family)
    else:
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
    font.setPointSize(point_size)
    font.setBold(bold)
    return font


def make_tray_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(24, 24, 28))
    painter.setPen(QColor(90, 200, 250))
    painter.drawRoundedRect(4, 6, 56, 52, 14, 14)
    painter.setPen(QColor(90, 200, 250))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(34)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "V")
    painter.end()
    return QIcon(pixmap)


def open_in_file_manager(path: str) -> None:
    if not path or not os.path.isdir(path):
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "--", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


class NotchWindow(QWidget):
    """The top-center overlay panel."""

    refresh_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, store: Store, font_family: Optional[str] = None) -> None:
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.store = store
        self.font_family = font_family
        self.pinned = False
        self.expanded = False
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(160)
        self._hover_timer.timeout.connect(lambda: self.set_expanded(True))
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(220)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_frame = 0
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(1000)
        self._ui_timer.timeout.connect(self._tick_ui)
        self._ui_timer.start()
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(14, 8, 14, 12)
        self.root_layout.setSpacing(6)

        self.compact_row = QWidget()
        compact_layout = QHBoxLayout(self.compact_row)
        compact_layout.setContentsMargins(8, 0, 8, 0)
        compact_layout.setSpacing(8)
        self.compact_source = QLabel("AGENT")
        self.compact_source.setFont(mono(font_family, 9, True))
        self.compact_task = QLabel("启动 Claude Code、Codex 或 ZCode；这里会自动出现。")
        self.compact_task.setFont(mono(font_family, 9))
        self.compact_pulse = QLabel("")
        self.compact_pulse.setFont(mono(font_family, 10))
        self.compact_overflow = QLabel("")
        self.compact_overflow.setFont(mono(font_family, 9))
        compact_layout.addWidget(self.compact_source)
        compact_layout.addWidget(self.compact_task, 1)
        compact_layout.addWidget(self.compact_overflow)
        compact_layout.addWidget(self.compact_pulse)

        self.expanded_panel = QWidget()
        expanded_layout = QVBoxLayout(self.expanded_panel)
        expanded_layout.setContentsMargins(6, 2, 6, 2)
        expanded_layout.setSpacing(6)

        title_row = QHBoxLayout()
        self.title_label = QLabel("会话中心")
        self.title_label.setFont(mono(font_family, 11, True))
        self.usage_summary = QLabel("")
        self.usage_summary.setFont(mono(font_family, 9))
        self.usage_summary.setCursor(Qt.PointingHandCursor)
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.usage_summary)
        expanded_layout.addLayout(title_row)

        self.sessions_host = QVBoxLayout()
        self.sessions_host.setSpacing(4)
        expanded_layout.addLayout(self.sessions_host)

        self.request_slot = QVBoxLayout()
        expanded_layout.addLayout(self.request_slot)

        footer = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        settings_btn = QPushButton("设置")
        quit_btn = QPushButton("退出")
        for btn in (refresh_btn, settings_btn, quit_btn):
            btn.setFont(mono(font_family, 9))
            btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_requested.emit)
        settings_btn.clicked.connect(self.settings_requested.emit)
        quit_btn.clicked.connect(self.quit_requested.emit)
        footer.addWidget(refresh_btn)
        footer.addWidget(settings_btn)
        footer.addStretch(1)
        footer.addWidget(quit_btn)
        expanded_layout.addLayout(footer)

        self.root_layout.addWidget(self.compact_row)
        self.root_layout.addWidget(self.expanded_panel)
        self.expanded_panel.setVisible(False)

        store.sessionsChanged.connect(self.rebuild)
        store.requestsChanged.connect(self.rebuild)
        store.usageChanged.connect(self.rebuild)
        store.pinRequested.connect(self.force_expand)
        self.set_compact()
        self.position_top_center()

    # ── geometry / state ──────────────────────────────────

    def position_top_center(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = geo.center().x() - self.width() // 2
        self.move(x, geo.top())

    def set_compact(self) -> None:
        self.expanded = False
        self.expanded_panel.setVisible(False)
        self.compact_row.setVisible(True)
        self.setFixedSize(COMPACT_W, COMPACT_H)
        self.position_top_center()

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self.expanded:
            return
        if not expanded and self.pinned:
            return
        if not expanded and self.store.current_request is not None:
            return  # approval pending keeps the panel open
        self.expanded = expanded
        self.compact_row.setVisible(not expanded)
        self.expanded_panel.setVisible(expanded)
        if expanded:
            self.rebuild()
            self.relayout()
        else:
            self.setFixedSize(COMPACT_W, COMPACT_H)
        self.position_top_center()

    def relayout(self) -> None:
        """Recompute the expanded window size around current content."""
        self.adjustSize()
        self.setFixedWidth(EXPANDED_W)
        height = min(self.sizeHint().height(), 640)
        self.setFixedSize(EXPANDED_W, height)

    def force_expand(self) -> None:
        self.pinned = True
        self.set_expanded(True)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_timer.stop()
        if not self.pinned and self.store.current_request is None:
            self.set_expanded(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.pinned = not self.pinned
            if self.pinned:
                self.set_expanded(True)
            elif not self.underMouse():
                self.set_expanded(False)
        super().mousePressEvent(event)

    # ── painting ──────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        rect = self.rect().adjusted(0, 0, -1, -1)
        radius = 16.0
        path.moveTo(rect.left(), rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - radius)
        path.quadTo(rect.right(), rect.bottom(), rect.right() - radius, rect.bottom())
        path.lineTo(rect.left() + radius, rect.bottom())
        path.quadTo(rect.left(), rect.bottom(), rect.left(), rect.bottom() - radius)
        path.closeSubpath()
        painter.setPen(BORDER)
        painter.setBrush(BG)
        painter.drawPath(path)
        painter.end()

    # ── content ───────────────────────────────────────────

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def rebuild(self) -> None:
        self._rebuild_compact()
        if not self.expanded:
            return
        self._clear_layout(self.sessions_host)
        sessions = list(self.store.sessions.values())
        sessions.sort(key=lambda s: s.last_update, reverse=True)
        for session in sessions[:6]:
            self.sessions_host.addWidget(SessionRow(session, self.font_family))
        if not sessions:
            empty = QLabel("启动 Claude Code、Codex 或 ZCode；这里会自动出现。")
            empty.setFont(mono(self.font_family, 9))
            empty.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
            self.sessions_host.addWidget(empty)
        self._rebuild_request()
        self._rebuild_usage()
        self.relayout()

    def _rebuild_compact(self) -> None:
        sessions = list(self.store.sessions.values())
        running = [s for s in sessions if s.running]
        active = running[0] if running else (sessions[0] if sessions else None)
        if active is not None:
            self.compact_source.setText(active.provider)
            self.compact_source.setStyleSheet(
                f"color:{source_color(active.source)};background:transparent")
            self.compact_task.setText(self._elide(active.task, 34))
            overflow = len(sessions) - 1
            self.compact_overflow.setText(f"+{overflow}" if overflow > 0 else "")
            if active.running:
                self._pulse_timer.start()
            else:
                self._pulse_timer.stop()
                self.compact_pulse.setText("")
        else:
            self.compact_source.setText("VIBE")
            self.compact_source.setStyleSheet("color:#5AC8FA;background:transparent")
            self.compact_task.setText("等待 Agent 启动…")
            self.compact_overflow.setText("")
            self._pulse_timer.stop()
            self.compact_pulse.setText("")

    def _rebuild_usage(self) -> None:
        usage = self.store.current_usage
        if usage is None:
            self.usage_summary.setText("")
            return
        providers = self.store.usage_providers
        dots = " · ".join(
            "●" if i == self.store.usage_index else "○"
            for i in range(len(providers))
        )
        self.usage_summary.setText(f"{usage.provider}  {usage.summary_line()}"
                                   + (f"  {dots}" if len(providers) > 1 else ""))

    def _rebuild_request(self) -> None:
        self._clear_layout(self.request_slot)
        request = self.store.current_request
        if request is None:
            return
        card = AskCard(request, self.font_family) if request.kind == "ask" \
            else ApprovalCard(request, self.font_family)
        card.decided.connect(self._on_card_decision)
        self.request_slot.addWidget(card)

    def _on_card_decision(self, request_id: str, action: str, answers) -> None:
        self.store.decide(request_id, action, answers)

    @staticmethod
    def _elide(text: str, limit: int) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _tick_pulse(self) -> None:
        self._pulse_frame = (self._pulse_frame + 1) % len(PULSE_FRAMES)
        self.compact_pulse.setText(PULSE_FRAMES[self._pulse_frame])
        self.compact_pulse.setStyleSheet("color:#5AC8FA;background:transparent")

    def _tick_ui(self) -> None:
        self.store.dismiss_stale_requests()
        if self.expanded:
            self.rebuild()
        else:
            self._rebuild_compact()


class SessionRow(QFrame):
    """One agent session line in the expanded panel."""

    def __init__(self, session, font_family: Optional[str]) -> None:
        super().__init__()
        self.session = session
        self.setFrameShape(QFrame.NoFrame)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        badge = QLabel(session.provider)
        badge.setFont(mono(font_family, 9, True))
        badge.setStyleSheet(
            f"color:{source_color(session.source)};background:transparent")
        badge.setFixedWidth(74)

        text_host = QVBoxLayout()
        text_host.setSpacing(1)
        title = QLabel(NotchWindow._elide(session.task, 40))
        title.setFont(mono(font_family, 10, True))
        preview = QLabel(NotchWindow._elide(session.preview or session.detail, 46)
                         or "—")
        preview.setFont(mono(font_family, 8))
        preview.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
        text_host.addWidget(title)
        text_host.addWidget(preview)
        text_host.addWidget(self._meta_label(session))

        status = QLabel("● 运行" if session.running else f"· {session.relative_time()}")
        status.setFont(mono(font_family, 8))
        status.setStyleSheet(
            f"color:{GREEN.name() if session.running else TEXT_MUTED.name()};"
            "background:transparent")
        status.setFixedWidth(52)

        layout.addWidget(badge)
        layout.addLayout(text_host, 1)
        layout.addWidget(status)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{session.provider} · {session.cwd or session.detail}")
        self.mouseDoubleClickEvent = lambda _e: open_in_file_manager(session.cwd)

    @staticmethod
    def _meta_label(session) -> QLabel:
        meta_parts = [p for p in (session.workspace_name, session.terminal) if p]
        meta = QLabel(" · ".join(meta_parts) or session.source)
        meta.setFont(mono(None, 8))
        meta.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
        return meta


class ApprovalCard(QFrame):
    """Blocking approval card with risk explanation and countdown."""

    decided = Signal(str, str, object)

    def __init__(self, request, font_family: Optional[str]) -> None:
        super().__init__()
        self.request = request
        self.setFrameShape(QFrame.StyledPanel)
        risk = request.risk
        self.setStyleSheet(
            f"QFrame {{ background: {BG_DARKER.name()};"
            f" border: 1px solid {RISK_COLORS[risk.level]}; border-radius: 10px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        head = QHBoxLayout()
        risk_badge = QLabel(RISK_LABELS[risk.level])
        risk_badge.setFont(mono(font_family, 9, True))
        risk_badge.setStyleSheet(
            f"color:#111;background:{RISK_COLORS[risk.level]};"
            "border-radius:4px;padding:1px 6px")
        agent = QLabel(f"{request.agent_name} · {request.tool_name or 'Tool'}")
        agent.setFont(mono(font_family, 9))
        agent.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
        self.countdown = QLabel(self._remaining())
        self.countdown.setFont(mono(font_family, 9, True))
        self.countdown.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
        head.addWidget(risk_badge)
        head.addWidget(agent, 1)
        head.addWidget(self.countdown)
        layout.addLayout(head)

        task = QLabel(NotchWindow._elide(request.task_name, 52))
        task.setFont(mono(font_family, 10, True))
        task.setWordWrap(True)
        layout.addWidget(task)

        if request.target_file:
            target = QLabel(request.target_file)
            target.setFont(mono(font_family, 8))
            target.setWordWrap(True)
            target.setStyleSheet(f"color:{TEXT_MUTED.name()};background:transparent")
            layout.addWidget(target)

        for reason in risk.reasons[:4]:
            row = QLabel(f"· {reason}")
            row.setFont(mono(font_family, 8))
            row.setStyleSheet(f"color:{RISK_COLORS[risk.level]};background:transparent")
            layout.addWidget(row)

        if request.diff:
            diff = QLabel(request.diff[:800])
            diff.setFont(mono(font_family, 8))
            diff.setWordWrap(True)
            diff.setStyleSheet(
                f"color:{TEXT_MUTED.name()};background:{BG.name()};"
                "border-radius:4px;padding:4px")
            scroll = QScrollArea()
            scroll.setWidget(diff)
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(88)
            scroll.setStyleSheet("QScrollArea{border:none;}")
            layout.addWidget(scroll)

        buttons = QHBoxLayout()
        deny = QPushButton("拒绝")
        allow = QPushButton("允许")
        deny.setFont(mono(font_family, 9))
        allow.setFont(mono(font_family, 9, True))
        deny.clicked.connect(lambda: self.decided.emit(request.id, "deny", None))
        allow.clicked.connect(lambda: self.decided.emit(request.id, "allow", None))
        buttons.addWidget(deny)
        buttons.addWidget(allow)
        pending_count = 0  # set by parent store externally when queueing
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _remaining(self) -> str:
        seconds = self.request.remaining_seconds()
        if seconds >= 3600:
            return f"{seconds // 3600}h"
        if seconds >= 60:
            return f"{int(seconds // 60)}m"
        return f"{seconds}s"

    def _tick(self) -> None:
        self.countdown.setText(self._remaining())


class AskCard(QFrame):
    """Multi-question AskUserQuestion card."""

    decided = Signal(str, str, object)

    def __init__(self, request, font_family: Optional[str]) -> None:
        super().__init__()
        self.request = request
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ background: {BG_DARKER.name()};"
            f" border: 1px solid {BORDER.name()}; border-radius: 10px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        head = QLabel(f"{request.agent_name} 需要你的输入")
        head.setFont(mono(font_family, 10, True))
        layout.addWidget(head)

        self._question_widgets = []
        for question in request.questions[:4]:
            header = QLabel(question.header)
            header.setFont(mono(font_family, 9, True))
            header.setStyleSheet(f"color:#5AC8FA;background:transparent")
            layout.addWidget(header)
            text = QLabel(NotchWindow._elide(question.question, 120))
            text.setWordWrap(True)
            text.setFont(mono(font_family, 9))
            layout.addWidget(text)
            options = []
            for option in question.options[:6]:
                if question.multi_select:
                    control = QCheckBox(option.label)
                else:
                    control = QRadioButton(option.label)
                control.setFont(mono(font_family, 9))
                control.setStyleSheet("color:#EBEBF0;")
                if option.description:
                    control.setToolTip(option.description)
                layout.addWidget(control)
                options.append((control, option))
            if options and not question.multi_select:
                options[0][0].setChecked(True)
            self._question_widgets.append((question, options))

        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        submit = QPushButton("提交")
        cancel.setFont(mono(font_family, 9))
        submit.setFont(mono(font_family, 9, True))
        cancel.clicked.connect(lambda: self.decided.emit(request.id, "cancel", None))
        submit.clicked.connect(self._submit)
        buttons.addWidget(cancel)
        buttons.addWidget(submit)
        layout.addLayout(buttons)

    def _submit(self) -> None:
        answers = {}
        for question, options in self._question_widgets:
            if question.multi_select:
                chosen = [option.label for control, option in options if control.isChecked()]
                if not chosen:
                    chosen = [options[0][1].label]
                answers[question.question] = chosen
            else:
                chosen = next((option.label for control, option in options
                               if control.isChecked()), options[0][1].label)
                answers[question.question] = chosen
        self.decided.emit(self.request.id, "answer", answers)


class SettingsDialog(QDialog):
    """Settings: notifications, usage polling, hook install, health."""

    def __init__(self, store: Store, font_family: Optional[str],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setFont(mono(font_family, 10))
        self.setWindowTitle("Vibe Center 设置")
        self.setFixedWidth(430)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.notif_check = QCheckBox("原生通知（等待输入 / 失败 / 回合完成）")
        self.notif_check.setChecked(store.notifications_enabled)
        self.notif_check.toggled.connect(store.set_notifications_enabled)
        layout.addWidget(self.notif_check)

        self.usage_check = QCheckBox("自动监测用量（Z.ai / 多 provider 轮询）")
        self.usage_check.setChecked(store.auto_start_usage)
        layout.addWidget(self.usage_check)

        self.history_check = QCheckBox("保留审批决策历史（仅存类别与结果，不含内容）")
        self.history_check.setChecked(store.history_enabled)
        layout.addWidget(self.history_check)

        hook_box = QFrame()
        hook_box.setFrameShape(QFrame.StyledPanel)
        hook_layout = QVBoxLayout(hook_box)
        hook_layout.setContentsMargins(8, 8, 8, 8)
        hook_label = QLabel("Claude Code Hook")
        hook_label.setFont(mono(font_family, 10, True))
        self.hook_status = QLabel("检查中…")
        self.hook_status.setWordWrap(True)
        self.hook_status.setFont(mono(font_family, 8))
        hook_buttons = QHBoxLayout()
        install_btn = QPushButton("安装 / 修复 Hook")
        uninstall_btn = QPushButton("移除")
        install_btn.clicked.connect(self._install_hook)
        uninstall_btn.clicked.connect(self._uninstall_hook)
        hook_buttons.addWidget(install_btn)
        hook_buttons.addWidget(uninstall_btn)
        hook_buttons.addStretch(1)
        hook_layout.addWidget(hook_label)
        hook_layout.addWidget(self.hook_status)
        hook_layout.addLayout(hook_buttons)
        layout.addWidget(hook_box)

        health_box = QFrame()
        health_box.setFrameShape(QFrame.StyledPanel)
        health_layout = QVBoxLayout(health_box)
        health_layout.setContentsMargins(8, 8, 8, 8)
        health_label = QLabel("服务状态")
        health_label.setFont(mono(font_family, 10, True))
        self.health_labels = {}
        for key, title in (("ipc", "IPC 服务"), ("scanner", "进程扫描"),
                           ("usage", "用量服务"), ("hook", "Hook")):
            row = QLabel(f"{title}：…")
            row.setFont(mono(font_family, 8))
            self.health_labels[key] = row
            health_layout.addWidget(row)
        layout.addWidget(health_box)

        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

        store.healthChanged.connect(self.refresh_health)
        self.refresh_health()

    def refresh_health(self) -> None:
        titles = {"ipc": "IPC 服务", "scanner": "进程扫描",
                  "usage": "用量服务", "hook": "Hook"}
        for key, label in self.health_labels.items():
            kind, detail = self.store.health.get(key, ("disabled", ""))
            colors = {"checking": "#5AC8FA", "ready": "#34D399",
                      "warning": "#FBBF24", "failed": "#EF4444",
                      "disabled": "#8C8C96"}
            label.setText(f"{titles[key]}：[{kind}] {detail}")
            label.setStyleSheet(f"color:{colors.get(kind, '#8C8C96')};")

    def showEvent(self, event) -> None:  # noqa: N802
        from . import hooks
        try:
            configured, required, _auth = hooks.coverage()
            if configured >= required:
                self.hook_status.setText("✓ 全部事件已接入 relay")
            elif configured:
                self.hook_status.setText(f"⚠ 已接入 {configured}/{required} 个事件，建议修复")
            else:
                self.hook_status.setText("未安装 — 审批卡片将不可用")
        except Exception as exc:
            self.hook_status.setText(f"检查失败：{exc}")
        super().showEvent(event)

    def _install_hook(self) -> None:
        from . import hooks
        ok, message = hooks.install()
        QMessageBox.information(self, "Hook", message)
        self.showEvent(None)

    def _uninstall_hook(self) -> None:
        from . import hooks
        _ok, message = hooks.uninstall()
        QMessageBox.information(self, "Hook", message)
        self.showEvent(None)


class TrayController:
    """Menu-bar / system-tray integration."""

    def __init__(self, app: QApplication, window: NotchWindow,
                 on_settings, on_quit) -> None:
        self.icon = QSystemTrayIcon(make_tray_icon(), app)
        menu = QMenu()
        show_action = menu.addAction("显示面板")
        show_action.triggered.connect(window.force_expand)
        refresh_action = menu.addAction("刷新会话")
        refresh_action.triggered.connect(window.refresh_requested.emit)
        settings_action = menu.addAction("设置…")
        settings_action.triggered.connect(on_settings)
        menu.addSeparator()
        quit_action = menu.addAction("退出 Vibe Center")
        quit_action.triggered.connect(on_quit)
        self.icon.setContextMenu(menu)
        self.icon.setToolTip("Vibe Center")
        self.icon.show()
