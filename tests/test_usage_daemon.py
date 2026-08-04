import contextlib
import importlib.util
import io
import json
import hmac
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path("/Volumes/RTL9210/workspace/claude-projects/vibe-island-app")
USAGE_DAEMON_PATH = ROOT / "usage-daemon.py"


def load_usage_daemon():
    spec = importlib.util.spec_from_file_location("usage_daemon_module", USAGE_DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class UsageDaemonTests(unittest.TestCase):
    def setUp(self):
        self.module = load_usage_daemon()

    def write_token(self, tmpdir: str, token_hex: str = "22" * 32) -> pathlib.Path:
        token_path = pathlib.Path(tmpdir) / "ipc-token"
        token_path.write_text(token_hex, encoding="utf-8")
        return token_path

    def test_read_zai_key_prefers_coding_plan_and_honors_env_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "zai": {"options": {"apiKey": "fallback-key"}},
                            "bigmodel-coding-plan-prod": {"options": {"apiKey": "preferred-key"}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"VIBE_ISLAND_USAGE_CONFIG": str(config_path)}, clear=False):
                self.assertEqual(self.module.read_zai_key(), "preferred-key")

    def test_poll_interval_uses_env_override_and_rejects_invalid_values(self):
        with mock.patch.dict(os.environ, {"VIBE_ISLAND_USAGE_POLL_INTERVAL": "15.5"}, clear=False):
            self.assertEqual(self.module.poll_interval_seconds(), 15.5)
        with mock.patch.dict(os.environ, {"VIBE_ISLAND_USAGE_POLL_INTERVAL": "-1"}, clear=False):
            self.assertEqual(self.module.poll_interval_seconds(), self.module.DEFAULT_POLL_INTERVAL)
        with mock.patch.dict(os.environ, {"VIBE_ISLAND_USAGE_POLL_INTERVAL": "oops"}, clear=False):
            self.assertEqual(self.module.poll_interval_seconds(), self.module.DEFAULT_POLL_INTERVAL)

    def test_parse_quota_extracts_windows_and_resets(self):
        fixed_now_seconds = 1_800_000_000
        response = {
            "code": 200,
            "data": {
                "level": "pro",
                "limits": [
                    {"unit": self.module.UNIT_FIVE_HOUR, "percentage": 12, "nextResetTime": (fixed_now_seconds + 50 * 60) * 1000},
                    {"unit": self.module.UNIT_SEVEN_DAY, "percentage": 34, "nextResetTime": (fixed_now_seconds + 2 * 86400 + 3 * 3600) * 1000},
                    {"unit": self.module.UNIT_MONTHLY, "percentage": 56, "nextResetTime": (fixed_now_seconds + 9 * 86400 + 4 * 3600) * 1000},
                ],
            },
        }

        with mock.patch.object(self.module.time, "time", return_value=fixed_now_seconds):
            usage = self.module.parse_quota(response)

        self.assertEqual(usage["five_hour"], 12)
        self.assertEqual(usage["five_hour_reset"], "50m")
        self.assertEqual(usage["seven_day"], 34)
        self.assertEqual(usage["seven_day_reset"], "2d3h")
        self.assertEqual(usage["monthly"], 56)
        self.assertEqual(usage["monthly_reset"], "9d4h")
        self.assertEqual(usage["level"], "pro")

    def test_poll_once_pushes_usage_and_ready_status(self):
        pushed_payloads = []

        def fake_fetch(url, _key):
            if url == self.module.QUOTA_URL:
                return {
                    "code": 200,
                    "data": {
                        "level": "pro",
                        "limits": [
                            {"unit": self.module.UNIT_FIVE_HOUR, "percentage": 1, "nextResetTime": None},
                            {"unit": self.module.UNIT_SEVEN_DAY, "percentage": 2, "nextResetTime": None},
                            {"unit": self.module.UNIT_MONTHLY, "percentage": 3, "nextResetTime": None},
                        ],
                    },
                }
            return {
                "code": 200,
                "data": [{"status": "VALID", "productName": "Pro", "billingCycle": "MONTHLY"}],
            }

        with mock.patch.object(self.module, "fetch_json", side_effect=fake_fetch):
            with mock.patch.object(self.module, "push_payload", side_effect=lambda payload: pushed_payloads.append(payload) or True):
                self.assertTrue(self.module.poll_once("secret-key"))

        self.assertEqual(pushed_payloads[0]["type"], "usage")
        self.assertEqual(pushed_payloads[0]["usage"]["provider"], "Z.ai")
        self.assertEqual(pushed_payloads[0]["usage"]["plan"], "Pro")
        self.assertEqual(pushed_payloads[1]["type"], "usage_status")
        self.assertEqual(pushed_payloads[1]["status"], "ready")

    def test_poll_once_reports_fetch_error_status(self):
        pushed_payloads = []

        with mock.patch.object(self.module, "fetch_json", return_value=None):
            with mock.patch.object(self.module, "push_payload", side_effect=lambda payload: pushed_payloads.append(payload) or True):
                self.assertFalse(self.module.poll_once("secret-key"))

        self.assertEqual(pushed_payloads, [self.module.build_status_payload("fetch_error", "quota_fetch_failed")])

    def test_fetch_json_logs_no_api_key_material_on_error(self):
        secret_key = "secret-key-should-not-appear"
        buffer = io.StringIO()

        with mock.patch.object(
            self.module.urllib.request,
            "urlopen",
            side_effect=self.module.urllib.error.URLError(secret_key),
        ):
            with contextlib.redirect_stdout(buffer):
                self.assertIsNone(self.module.fetch_json(self.module.QUOTA_URL, secret_key))

        output = buffer.getvalue()
        self.assertIn("fetch error", output)
        self.assertNotIn(secret_key, output)

    def test_push_payload_signs_usage_messages(self):
        sent = {}
        real_socket = self.module.socket.socket

        class FakeSocket:
            def settimeout(self, _timeout):
                return None

            def connect(self, _address):
                return None

            def sendall(self, payload):
                sent["payload"] = json.loads(payload.decode("utf-8").strip())

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        self.module.socket.socket = lambda *args, **kwargs: FakeSocket()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_path = self.write_token(tmp)
                with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                    self.assertTrue(self.module.push_payload({"type": "usage_status", "status": "ready"}))
                    self.assertIsNone(self.module.verify_payload(sent["payload"]))
        finally:
            self.module.socket.socket = real_socket

        payload = sent["payload"]
        self.assertIn("auth_nonce", payload)
        self.assertIn("auth_signature", payload)

    def test_push_payload_logs_auth_error_without_token_material(self):
        secret_token = "ab" * 32
        buffer = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            token_path = self.write_token(tmp, secret_token)
            token_path.write_text("not-hex", encoding="utf-8")
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_IPC_TOKEN_FILE": str(token_path)}, clear=False):
                with contextlib.redirect_stdout(buffer):
                    self.assertFalse(self.module.push_payload({"type": "usage_status", "status": "ready"}))

        output = buffer.getvalue()
        self.assertIn("ipc auth unavailable", output)
        self.assertNotIn(secret_token, output)

    def test_acquire_single_instance_lock_writes_pid_and_blocks_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = pathlib.Path(tmp) / "usage-daemon.lock"
            handle = self.module.acquire_single_instance_lock(str(lock_path))
            self.assertIsNotNone(handle)
            try:
                self.assertEqual(lock_path.read_text(encoding="utf-8").strip(), str(os.getpid()))
                result = subprocess.run(
                    [
                        "python3",
                        "-c",
                        (
                            "import importlib.util, pathlib; "
                            f"path = pathlib.Path({str(USAGE_DAEMON_PATH)!r}); "
                            "spec = importlib.util.spec_from_file_location('usage_daemon_child', path); "
                            "module = importlib.util.module_from_spec(spec); "
                            "spec.loader.exec_module(module); "
                            f"handle = module.acquire_single_instance_lock({str(lock_path)!r}); "
                            "print('acquired' if handle else 'blocked')"
                        ),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            finally:
                handle.close()

        self.assertEqual(result.stdout.strip(), "blocked")

    def test_main_reports_already_running_when_lock_is_held(self):
        with mock.patch.object(self.module, "acquire_single_instance_lock", return_value=None):
            with mock.patch.object(self.module, "push_status") as push_status:
                self.assertEqual(self.module.main(), 0)

        push_status.assert_called_once_with("already_running")


if __name__ == "__main__":
    unittest.main()
