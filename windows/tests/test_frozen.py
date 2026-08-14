"""Frozen-mode (PyInstaller packaging) support tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from vibecenter import frozen  # noqa: E402
from vibecenter import hooks  # noqa: E402


class FrozenHelperTests(unittest.TestCase):
    def test_ensure_relay_runtime_stages_py_and_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, "bundle")
            exe_root = os.path.join(tmp, "exedir")
            runtime = os.path.join(tmp, "runtime-bin")
            os.makedirs(bundle)
            os.makedirs(exe_root)
            with open(os.path.join(bundle, "relay.py"), "w", encoding="utf-8") as fh:
                fh.write("# relay\n")
            with open(os.path.join(exe_root, frozen.RELAY_EXE_NAME), "wb") as fh:
                fh.write(b"MZfake")

            old = os.environ.get("VIBE_ISLAND_RUNTIME_BIN_DIR")
            os.environ["VIBE_ISLAND_RUNTIME_BIN_DIR"] = runtime
            try:
                relay_py, relay_exe = frozen.ensure_relay_runtime(
                    bundle_root=bundle, exe_root=exe_root)
            finally:
                if old is None:
                    os.environ.pop("VIBE_ISLAND_RUNTIME_BIN_DIR", None)
                else:
                    os.environ["VIBE_ISLAND_RUNTIME_BIN_DIR"] = old

            self.assertEqual(relay_py, os.path.join(runtime, "relay.py"))
            self.assertEqual(relay_exe, os.path.join(runtime, frozen.RELAY_EXE_NAME))
            self.assertTrue(os.path.isfile(relay_py))
            self.assertTrue(os.path.isfile(relay_exe))

    def test_ensure_relay_runtime_no_exe_when_not_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, "bundle")
            os.makedirs(bundle)
            with open(os.path.join(bundle, "relay.py"), "w", encoding="utf-8") as fh:
                fh.write("# relay\n")
            os.environ["VIBE_ISLAND_RUNTIME_BIN_DIR"] = os.path.join(tmp, "rt")
            try:
                relay_py, relay_exe = frozen.ensure_relay_runtime(
                    bundle_root=bundle, exe_root=tmp)
            finally:
                os.environ.pop("VIBE_ISLAND_RUNTIME_BIN_DIR", None)
            self.assertTrue(relay_py)
            self.assertEqual(relay_exe, "")

    def test_load_usage_daemon_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "usage-daemon.py")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("POLL_SECONDS = 42\n\ndef poll_all_providers():\n    return True\n")
            original = frozen.bundle_dir
            frozen.bundle_dir = lambda: tmp  # type: ignore[assignment]
            try:
                module = frozen.load_usage_daemon_module()
            finally:
                frozen.bundle_dir = original  # type: ignore[assignment]
            self.assertEqual(module.POLL_SECONDS, 42)
            self.assertTrue(module.poll_all_providers())


class HookCommandTests(unittest.TestCase):
    def test_relay_command_uses_python_in_source_mode(self) -> None:
        command = hooks.relay_command()
        self.assertIn("relay.py", command)
        self.assertIn(sys.executable, command)

    def test_detection_accepts_frozen_exe_commands(self) -> None:
        self.assertTrue(hooks._looks_like_relay_command(
            '"C:\\Users\\bruce\\.vibe-island\\bin\\VibeCenterRelay.exe"'))
        self.assertTrue(hooks._looks_like_relay_command(
            '"python3" "/Users/bruce/Scripts/vibe-island/relay.py"'))
        self.assertFalse(hooks._looks_like_relay_command("echo hello"))
        self.assertFalse(hooks._looks_like_relay_command("C:\\tools\\relay.py"))

    def test_install_and_uninstall_roundtrip_with_exe_style_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.json")
            os.environ["CLAUDE_SETTINGS"] = settings
            try:
                ok, _ = hooks.install()
                self.assertTrue(ok)
                with open(settings, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                entries = cfg["hooks"]["PreToolUse"]
                self.assertTrue(any(e.get("matcher") for e in entries))

                # Simulate a frozen exe-style command, then verify a fresh
                # install replaces it (no duplicates).
                cfg["hooks"]["PreToolUse"].append({
                    "hooks": [{"type": "command", "command":
                               '"C:\\Users\\x\\.vibe-island\\bin\\VibeCenterRelay.exe"'}]})
                with open(settings, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh)
                hooks.install()
                with open(settings, "r", encoding="utf-8") as fh:
                    cfg2 = json.load(fh)
                self.assertEqual(len(cfg2["hooks"]["PreToolUse"]), 1)

                ok, _ = hooks.uninstall()
                self.assertTrue(ok)
                with open(settings, "r", encoding="utf-8") as fh:
                    cfg3 = json.load(fh)
                self.assertNotIn("hooks", cfg3)
            finally:
                os.environ.pop("CLAUDE_SETTINGS", None)


if __name__ == "__main__":
    unittest.main()
