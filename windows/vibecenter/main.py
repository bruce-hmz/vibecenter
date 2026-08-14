"""Vibe Center entry point — wires IPC, scanner, watcher, tray, notch UI.

Usage:
    python -m vibecenter.main            # run the panel
    python -m vibecenter.main --self-test
        Offscreen render of compact / expanded / approval / ask states to
        windows/build/selftest-*.png, then exit (works on any OS).

Run from the repo: python windows/vibecenter/main.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Dict, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Allow running as a plain script (python windows/vibecenter/main.py)
# in addition to python -m vibecenter.main.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vibecenter"

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import hooks as hooks_mod
from . import scanner
from .ipc import IPCServer
from .models import AgentSession, AskQuestion, AskOption, PendingRequest, UsageSnapshot
from .store import Store
from .ui import NotchWindow, SettingsDialog, TrayController, load_app_font, mono

SCAN_INTERVAL_MS = 5000
USAGE_ROTATE_MS = 8000
IDLE_SECS = 20


class AppController:
    """Owns every moving part and routes IPC traffic into the store."""

    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.store = Store()
        self.font_family = load_app_font()
        self.window = NotchWindow(self.store, self.font_family)
        self.settings_dialog: Optional[SettingsDialog] = None
        self.server = IPCServer(
            on_session=self._on_session,
            on_compact=self._on_compact,
            on_usage=self._on_usage,
            on_usage_status=self._on_usage_status,
            on_request=self._on_request,
        )
        self.file_watchers: Dict[str, object] = {}
        self.running_timeouts: Dict[str, QTimer] = {}
        self.usage_process: Optional[subprocess.Popen] = None
        self.usage_stop = None  # threading.Event for the in-process daemon
        self._scan_in_flight = False

        self.scan_timer = QTimer()
        self.scan_timer.setInterval(SCAN_INTERVAL_MS)
        self.scan_timer.timeout.connect(self.scan)
        self.usage_rotate_timer = QTimer()
        self.usage_rotate_timer.setInterval(USAGE_ROTATE_MS)
        self.usage_rotate_timer.timeout.connect(self.store.advance_usage)

        self.window.refresh_requested.connect(self.scan)
        self.window.settings_requested.connect(self.open_settings)
        self.window.quit_requested.connect(self.quit)
        self.store.notifyRequest.connect(self._maybe_notify_request)
        self.store.sessionEvent.connect(self._maybe_notify_event)

    # ── IPC callbacks (server threads → queued into the GUI thread) ──

    def start(self) -> bool:
        ok = self.server.start()
        if ok:
            self.store.set_health("ipc", "ready", f"TCP {self.server.port} 已监听")
        else:
            self.store.set_health("ipc", "failed", f"端口 {self.server.port} 启动失败")
        self.scan()
        self.scan_timer.start()
        self.usage_rotate_timer.start()
        self._refresh_hook_health()
        if self.store.auto_start_usage:
            self.start_usage_daemon()
        return ok

    # ── session/usage plumbing ────────────────────────────

    def _on_session(self, message: dict) -> None:
        QTimer.singleShot(0, lambda: self.store.apply_session_message(message))

    def _on_compact(self, message: dict) -> None:
        def apply() -> None:
            self.store.compact_task = str(message.get("task") or "")
            self.store.compact_agent = str(message.get("agent") or "")
        QTimer.singleShot(0, apply)

    def _on_usage(self, message: dict) -> None:
        QTimer.singleShot(0, lambda: self.store.apply_usage(message))

    def _on_usage_status(self, message: dict) -> None:
        status = str(message.get("status") or "fetch_error")
        detail = str(message.get("detail") or status)
        mapping = {
            "starting": ("checking", "正在启动配额服务"),
            "ready": ("ready", "配额服务已连接"),
            "unconfigured": ("warning", "未找到 Z.ai API 配置"),
            "already_running": ("warning", "已有用量服务实例正在运行"),
        }
        kind, text = mapping.get(status, ("failed", f"配额刷新失败：{detail}"))
        QTimer.singleShot(0, lambda: self.store.set_health("usage", kind, text))

    def _on_request(self, request, held) -> None:
        QTimer.singleShot(0, lambda: self.store.enqueue_request(request, held))

    # ── scanning + file watching ──────────────────────────

    def scan(self) -> None:
        if self._scan_in_flight:
            return
        self._scan_in_flight = True

        def work() -> None:
            sessions = scanner.scan_all()
            def apply() -> None:
                removed = self.store.reconcile_scanned(sessions)
                for session_id in removed:
                    self.file_watchers.pop(session_id, None)
                    timeout = self.running_timeouts.pop(session_id, None)
                    if timeout is not None:
                        timeout.stop()
                for session in sessions:
                    self._watch_session(session)
                self._scan_in_flight = False
                self.store.set_health(
                    "scanner", "ready", f"发现 {len(sessions)} 个会话")
            QTimer.singleShot(0, apply)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _watch_session(self, session) -> None:
        if session.id in self.file_watchers or not session.transcript_path:
            return
        if not os.path.exists(session.transcript_path):
            return
        from PySide6.QtCore import QFileSystemWatcher
        watcher = QFileSystemWatcher([session.transcript_path])
        watcher.fileChanged.connect(
            lambda path, sid=session.id, source=session.source:
            self._on_file_changed(sid, source, path))
        self.file_watchers[session.id] = watcher

    def _on_file_changed(self, session_id: str, source: str, path: str) -> None:
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        session.running = True
        preview = scanner.read_preview(path, source)
        if preview:
            session.preview = preview
        self.store.sessionsChanged.emit()
        # Watchers drop files after atomic replaces — re-add.
        watcher = self.file_watchers.get(session_id)
        if watcher is not None and path not in watcher.files():
            if os.path.exists(path):
                watcher.addPath(path)
        old = self.running_timeouts.pop(session_id, None)
        if old is not None:
            old.stop()
        timer = QTimer(self.window)
        timer.setSingleShot(True)
        timer.setInterval(IDLE_SECS * 1000)
        timer.timeout.connect(
            lambda sid=session_id: self._mark_idle(sid))
        timer.start()
        self.running_timeouts[session_id] = timer

    def _mark_idle(self, session_id: str) -> None:
        session = self.store.sessions.get(session_id)
        if session is not None:
            session.running = False
            self.store.sessionsChanged.emit()
        self.running_timeouts.pop(session_id, None)

    # ── usage daemon ──────────────────────────────────────

    def start_usage_daemon(self) -> None:
        from . import frozen as frozen_mod

        if frozen_mod.is_frozen():
            self._start_inprocess_usage_daemon()
            return
        if self.usage_process is not None and self.usage_process.poll() is None:
            return
        script = os.path.join(REPO_ROOT, "usage-daemon.py")
        if not os.path.exists(script):
            self.store.set_health("usage", "failed", "未找到 usage-daemon.py")
            return
        try:
            self.usage_process = subprocess.Popen(
                [sys.executable, script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.store.set_health("usage", "checking", "正在启动配额服务")
        except OSError as exc:
            self.store.set_health("usage", "failed", f"无法启动配额服务：{exc}")

    def _start_inprocess_usage_daemon(self) -> None:
        """Frozen build: run usage-daemon's loop in a worker thread.

        No Python installation exists on the target machine, so the
        bundled usage-daemon.py is loaded as a module and driven here.
        """
        if self.usage_stop is not None:
            return
        import threading

        from . import frozen as frozen_mod

        try:
            daemon = frozen_mod.load_usage_daemon_module()
        except (OSError, ImportError) as exc:
            self.store.set_health("usage", "failed", f"无法加载配额服务：{exc}")
            return
        self.usage_stop = threading.Event()
        self.store.set_health("usage", "checking", "正在启动配额服务")

        def loop(stop: threading.Event) -> None:
            daemon.push_status("starting")
            while not stop.is_set():
                daemon.poll_codex_usage()
                polled = daemon.poll_all_providers()
                if polled:
                    self.store.set_health("usage", "ready", "配额服务已连接")
                else:
                    key = daemon.read_zai_key()
                    if not key:
                        self.store.set_health("usage", "warning", "未找到 Z.ai API 配置")
                    else:
                        daemon.push_status("fetch_error", "no provider returned usage data")
                stop.wait(daemon.poll_interval_seconds())

        threading.Thread(target=loop, args=(self.usage_stop,), daemon=True,
                         name="vibecenter-usage").start()

    def stop_usage_daemon(self) -> None:
        if self.usage_stop is not None:
            self.usage_stop.set()
            self.usage_stop = None
        if self.usage_process is not None:
            self.usage_process.terminate()
            self.usage_process = None
        self.store.set_health("usage", "disabled", "用量监测已关闭")

    def restart_usage_daemon(self) -> None:
        if self.store.auto_start_usage:
            self.start_usage_daemon()

    def on_auto_usage_changed(self, enabled: bool) -> None:
        self.store.set_auto_start_usage(enabled)
        if enabled:
            self.start_usage_daemon()
        else:
            self.stop_usage_daemon()

    # ── hook health ───────────────────────────────────────

    def _refresh_hook_health(self) -> None:
        try:
            configured, required, _auth = hooks_mod.coverage()
            if configured >= required:
                self.store.set_health("hook", "ready", "Claude Code Hook 已就绪")
            elif configured:
                missing = ", ".join(hooks_mod.missing_events()[:3])
                self.store.set_health("hook", "warning",
                                      f"Hook 部分接入（缺 {missing}…），建议修复")
            else:
                self.store.set_health("hook", "warning", "未安装 Hook — 设置里可一键安装")
        except Exception as exc:
            self.store.set_health("hook", "failed", f"Hook 检查失败：{exc}")

    # ── notifications ─────────────────────────────────────

    def _maybe_notify_request(self, request) -> None:
        if not self.store.notifications_enabled:
            return
        title = "Agent 等待你的决定" if request.kind == "approval" else "Agent 需要你的输入"
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.icon.showMessage(title, f"{request.agent_name} · {request.source}",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)

    def _maybe_notify_event(self, kind: str, session_id: str) -> None:
        if not self.store.notifications_enabled:
            return
        titles = {"completed": "Agent 回合已完成",
                  "failed": "Agent 执行失败",
                  "waiting": "Agent 正在等待你"}
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.icon.showMessage(titles.get(kind, "Agent"), "",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)

    # ── misc ──────────────────────────────────────────────

    def open_settings(self) -> None:
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self.store, self.font_family)
            self.settings_dialog.usage_check.toggled.connect(self.on_auto_usage_changed)
            self.settings_dialog.history_check.toggled.connect(
                self.store.set_history_enabled)
        self.settings_dialog.show()
        self.settings_dialog.raise_()

    def quit(self) -> None:
        self.stop_usage_daemon()
        self.server.stop()
        self.app.quit()


def run() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("Vibe Center")
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    controller.start()
    tray = TrayController(app, controller.window,
                          on_settings=controller.open_settings,
                          on_quit=controller.quit)
    controller.tray = tray
    controller.window.show()
    ret = app.exec()
    controller.server.stop()
    return ret


def self_test() -> int:
    """Offscreen render of the four UI states for visual verification."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    out_dir = os.path.join(REPO_ROOT, "windows", "build")
    os.makedirs(out_dir, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Vibe Center")

    controller = AppController(app)
    store = controller.store
    print("font family:", controller.font_family)

    now = time.time()
    store.sessions = {
        "s1": AgentSession(id="s1", source="zcode", task="修复登录页样式并补齐测试",
                           preview="正在编辑 src/login.css…", detail="vibe-island-app",
                           cwd="/Users/bruce/work/vibe-island-app", terminal="zcode",
                           running=True, last_update=now),
        "s2": AgentSession(id="s2", source="claude", task="重构数据库迁移脚本",
                           preview="tool: Edit", detail="api-server",
                           cwd="/Users/bruce/work/api-server", terminal="warp",
                           running=False, last_update=now - 180),
        "s3": AgentSession(id="s3", source="codex", task="为攻略站生成 SEO 长尾页",
                           preview="已生成 12 个页面，正在写入 sitemap…",
                           detail="game-site", cwd="/Users/bruce/work/game-site",
                           running=True, last_update=now - 20),
        "s4": AgentSession(id="s4", source="gemini", task="调研竞品定价策略",
                           preview="总结：三家均按席位收费…", detail="research",
                           running=False, last_update=now - 3600),
        "s5": AgentSession(id="s5", source="kimi", task="写单元测试覆盖支付模块",
                           preview="已新增 14 个用例", running=False, last_update=now - 7200),
    }
    store.usage_providers = [
        UsageSnapshot(provider="Z.ai", five_hour=6, five_hour_reset="53m",
                      seven_day=41, monthly=214, level="plus", plan="Max"),
        UsageSnapshot(provider="Codex", five_hour=2, five_hour_reset="1h12m",
                      monthly=88, plan="Pro"),
    ]
    store.sessionsChanged.emit()
    store.usageChanged.emit()

    window = controller.window

    def capture(name: str) -> None:
        pixmap = window.grab()
        path = os.path.join(out_dir, f"selftest-{name}.png")
        saved = pixmap.save(path)
        print(f"saved {path}: {saved} ({pixmap.width()}x{pixmap.height()})")

    window.set_compact()
    capture("compact")

    window.pinned = True
    window.set_expanded(True)
    capture("expanded")

    # approval card
    request = PendingRequest(
        id="req-1", kind="approval", source="claude", agent_name="Claude Code",
        task_name="Edit file", target_file="/Users/bruce/work/api-server/migrations/002.sql",
        tool_name="Edit", cwd="/Users/bruce/work/api-server",
        diff="--- before\nINSERT INTO users…\n+++ after\nINSERT INTO users_new…",
        arrived_at=now, expires_at=now + 300)
    from .ipc import HeldRequest
    store.enqueue_request(request, HeldRequest({}))
    window.set_expanded(True)
    capture("approval")
    store.decide("req-1", "deny")

    # ask card
    ask = PendingRequest(
        id="req-2", kind="ask", source="zcode", agent_name="ZCode",
        task_name="Question", arrived_at=now, expires_at=now + 300,
        questions=[AskQuestion(
            id="q1", header="部署方式", question="这次变更希望怎么发布？",
            options=[AskOption(id="a", label="直接上线"),
                     AskOption(id="b", label="先发预览环境"),
                     AskOption(id="c", label="只提交不发")])])
    store.enqueue_request(ask, HeldRequest({}))
    window.set_expanded(True)
    capture("ask")
    return 0


def _ensure_streams() -> None:
    """PyInstaller --windowed sets sys.stdout/stderr to None; print would
    crash. Point them at the null device so --self-test logging works."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except OSError:
                setattr(sys, name, None)


def main() -> int:
    _ensure_streams()
    if "--self-test" in sys.argv:
        return self_test()
    return run()


if __name__ == "__main__":
    sys.exit(main())
