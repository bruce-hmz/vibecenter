"""Frozen (PyInstaller) runtime support.

When the Windows app is packaged with PyInstaller --onefile, the repo's
loose files (relay.py, usage-daemon.py, the font) live inside the
temporary extraction dir (sys._MEIPASS). This module resolves bundled
resources, stages relay assets to a stable directory (~/.vibe-island/bin)
so hook commands survive restarts, and loads usage-daemon.py as an
in-process module (no Python installation required on the target).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from typing import Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RELAY_EXE_NAME = "VibeCenterRelay.exe"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Where PyInstaller put the bundled data files."""
    if is_frozen():
        return str(getattr(sys, "_MEIPASS", "")) or os.path.dirname(
            os.path.abspath(sys.executable))
    return REPO_ROOT


def exe_dir() -> str:
    """Directory of the running executable (frozen) or the repo root."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return REPO_ROOT


def bundled_path(name: str) -> str:
    return os.path.join(bundle_dir(), name)


def runtime_bin_dir() -> str:
    override = os.environ.get("VIBE_ISLAND_RUNTIME_BIN_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".vibe-island", "bin")


def _copy_if_newer(src: str, dest: str) -> None:
    try:
        if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src) \
                and os.path.getsize(dest) == os.path.getsize(src):
            return
        shutil.copy2(src, dest)
    except OSError:
        pass


def ensure_relay_runtime(bundle_root: Optional[str] = None,
                         exe_root: Optional[str] = None) -> Tuple[str, str]:
    """Stage relay.py (+ relay exe when shipped) into ~/.vibe-island/bin.

    Hook commands in ~/.claude/settings.json must point at a stable path,
    so the bundled relay assets are copied out on every launch (cheap —
    skipped when already up to date). Returns (relay_py, relay_exe_or_"").
    """
    bundle_root = bundle_root if bundle_root is not None else bundle_dir()
    exe_root = exe_root if exe_root is not None else exe_dir()
    dest = runtime_bin_dir()
    os.makedirs(dest, exist_ok=True)

    relay_py = os.path.join(dest, "relay.py")
    src_py = os.path.join(bundle_root, "relay.py")
    if os.path.isfile(src_py):
        _copy_if_newer(src_py, relay_py)

    relay_exe = ""
    src_exe = os.path.join(exe_root, RELAY_EXE_NAME)
    if os.path.isfile(src_exe):
        relay_exe = os.path.join(dest, RELAY_EXE_NAME)
        _copy_if_newer(src_exe, relay_exe)
    return relay_py, relay_exe


def load_usage_daemon_module():
    """Import usage-daemon.py (dash in name → spec loader, works frozen)."""
    path = bundled_path("usage-daemon.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location("vibecenter_usage_daemon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module
