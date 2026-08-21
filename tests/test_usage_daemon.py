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


ROOT = pathlib.Path(__file__).resolve().parent.parent
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


class GeminiPlanTests(unittest.TestCase):
    def setUp(self):
        self.module = load_usage_daemon()
        self.module._gemini_token_cache.update({"access_token": None, "expiry_date": 0})

    def test_snapshot_maps_paid_tier_and_sums_g1_credits(self):
        resp = {
            "currentTier": {"id": "standard-tier"},
            "paidTier": {"availableCredits": [
                {"creditType": "GOOGLE_ONE_AI", "creditAmount": "1000"},
                {"creditType": "OTHER", "creditAmount": "42"},
                {"creditType": "GOOGLE_ONE_AI", "creditAmount": "240.5"},
            ]},
        }
        snapshot = self.module.gemini_snapshot_from_response(resp)
        self.assertEqual(snapshot["provider"], "Gemini")
        self.assertEqual(snapshot["plan"], "Google AI Pro")
        self.assertEqual(snapshot["level"], "standard-tier")
        self.assertEqual(snapshot["credits"], "1,240")

    def test_snapshot_free_tier_has_no_credits(self):
        resp = {"currentTier": {"id": "free-tier"}}
        snapshot = self.module.gemini_snapshot_from_response(resp)
        self.assertEqual(snapshot["plan"], "Gemini Free")
        self.assertNotIn("credits", snapshot)

    def test_poll_skips_when_never_logged_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_GEMINI_OAUTH_CREDS":
                                  os.path.join(tmp, "none.json")}):
                self.assertIsNone(self.module.poll_gemini_plan())

    def test_poll_pushes_signed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds_path = os.path.join(tmp, "oauth_creds.json")
            pathlib.Path(creds_path).write_text(json.dumps({
                "access_token": "tok", "expiry_date": 9_999_999_999_999,
                "refresh_token": "r"}), encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_GEMINI_OAUTH_CREDS": creds_path}):
                with mock.patch.object(
                        self.module, "cloudcode_health_check",
                        return_value={"currentTier": {"id": "legacy-paid-tier"},
                                      "paidTier": {"availableCredits": [
                                          {"creditType": "GOOGLE_ONE_AI",
                                           "creditAmount": "1500"}]}}) as check:
                    with mock.patch.object(self.module, "push_usage",
                                           return_value=True) as push:
                        snapshot = self.module.poll_gemini_plan()
                check.assert_called_once_with("tok")
        self.assertEqual(snapshot["plan"], "Google AI Ultra")
        self.assertEqual(snapshot["credits"], "1,500")
        pushed = push.call_args[0][0]
        self.assertEqual(pushed["provider"], "Gemini")

    def test_refresh_writes_back_updated_creds(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds_path = os.path.join(tmp, "oauth_creds.json")
            pathlib.Path(creds_path).write_text(json.dumps({
                "refresh_token": "r-old"}), encoding="utf-8")
            env = {"VIBE_ISLAND_GEMINI_OAUTH_CREDS": creds_path,
                   "VIBE_ISLAND_GEMINI_CLIENT_ID": "test-client",
                   "VIBE_ISLAND_GEMINI_CLIENT_SECRET": "test-secret"}
            with mock.patch.dict(os.environ, env):
                self.module._gemini_oauth_client_cache.clear()
                with mock.patch.object(
                        self.module, "http_post_json",
                        return_value={"access_token": "fresh",
                                      "expires_in": 3600}):
                    token = self.module.refresh_gemini_token(
                        {"refresh_token": "r-old"})
            self.assertEqual(token, "fresh")
            stored = json.loads(pathlib.Path(creds_path).read_text())
            self.assertEqual(stored["access_token"], "fresh")
            self.assertGreater(stored["expiry_date"], 0)

    def test_oauth_client_discovered_from_gemini_cli_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = pathlib.Path(tmp) / "bundle"
            bundle.mkdir()
            (bundle / "chunk-TEST.js").write_text(
                'var OAUTH_CLIENT_ID = '
                '"1234567890-abcdefabcdefabcdefabcdef'
                '.apps.googleusercontent.com";\n'
                'var OAUTH_CLIENT_SECRET = "GOCSPX-testsecret1234567890";\n',
                encoding="utf-8")
            # Adjacent-but-wrong pair (gcloud SDK client, no local secret)
            (bundle / "chunk-OTHER.js").write_text(
                'exports2.CLOUD_SDK_CLIENT_ID = "764086051850-6qr4p6gpi6hn5"'
                '06pt8ejuq83di341hur.apps.googleusercontent.com";',
                encoding="utf-8")
            with mock.patch.dict(
                    os.environ,
                    {"VIBE_ISLAND_GEMINI_CLI_DIRS": str(bundle),
                     "VIBE_ISLAND_GEMINI_CLIENT_ID": "",
                     "VIBE_ISLAND_GEMINI_CLIENT_SECRET": ""}):
                with mock.patch.object(self.module, "GEMINI_OAUTH_CONFIG",
                                       os.path.join(tmp, "no-oauth.json")):
                    self.module._gemini_oauth_client_cache.clear()
                    client = self.module.discover_gemini_oauth_client()
        self.assertEqual(client["client_id"],
                         "1234567890-abcdefabcdefabcdefabcdef"
                         ".apps.googleusercontent.com")
        self.assertEqual(client["client_secret"],
                         "GOCSPX-testsecret1234567890")

    def test_oauth_client_env_override_wins(self):
        with mock.patch.dict(
                os.environ,
                {"VIBE_ISLAND_GEMINI_CLIENT_ID": "env-id",
                 "VIBE_ISLAND_GEMINI_CLIENT_SECRET": "env-secret"}):
            self.module._gemini_oauth_client_cache.clear()
            client = self.module.discover_gemini_oauth_client()
        self.assertEqual((client["client_id"], client["client_secret"]),
                         ("env-id", "env-secret"))


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.module = load_usage_daemon()
        # Isolate from this machine's real opencodex cache so rollout
        # parsing is what's under test; cache-preference tests point the
        # env at explicit fixtures below.
        patcher = mock.patch.dict(
            os.environ, {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": "/nonexistent"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _rollout(self, tmpdir, name, mtime, rate):
        path = pathlib.Path(tmpdir) / "sessions" / "2026" / "08" / "15" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [{"type": "session_meta", "payload": {"id": name}}]
        if rate is not None:
            records.append({"type": "world_state",
                            "payload": {"rate_limits": rate}})
        else:
            records.append({"type": "world_state",
                            "payload": {"rate_limits": {
                                "primary": None, "secondary": None}}})
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                        encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_falls_back_to_older_file_when_active_rollout_has_null_limits(self):
        # Regression: the CLI leaves rate_limits null in the file it is
        # actively writing; the real weekly value sat in a 2-day-old file
        # that the old 24h mtime cutoff filtered out.
        import time

        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "rollout-active.jsonl", now - 60, None)
            self._rollout(tmp, "rollout-stale.jsonl", now - 2 * 86400, {
                "primary": {"used_percent": 3.0, "window_minutes": 10080,
                            "resets_at": now + 3600},
                "credits": {"unlimited": False},
            })
            with mock.patch.object(self.module, "CODEX_SESSIONS_DIR",
                                   os.path.join(tmp, "sessions")):
                usage = self.module.read_codex_usage()
        self.assertEqual(usage["provider"], "Codex")
        self.assertEqual(usage["seven_day"], 3)
        self.assertEqual(usage["plan"], "Codex")

    def test_prefers_record_with_future_reset_over_expired_window(self):
        import time

        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            # Newer file carries an expired window; older file has the
            # live one. The live one must win.
            self._rollout(tmp, "rollout-new-expired.jsonl", now - 600, {
                "primary": {"used_percent": 99.0, "window_minutes": 10080,
                            "resets_at": now - 100}})
            self._rollout(tmp, "rollout-old-live.jsonl", now - 5 * 86400, {
                "primary": {"used_percent": 12.0, "window_minutes": 10080,
                            "resets_at": now + 7200}})
            with mock.patch.object(self.module, "CODEX_SESSIONS_DIR",
                                   os.path.join(tmp, "sessions")):
                usage = self.module.read_codex_usage()
        self.assertEqual(usage["seven_day"], 12)

    def test_skips_files_older_than_the_weekly_window(self):
        import time

        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "rollout-ancient.jsonl", now - 9 * 86400, {
                "primary": {"used_percent": 50.0, "window_minutes": 10080,
                            "resets_at": now + 3600}})
            with mock.patch.object(self.module, "CODEX_SESSIONS_DIR",
                                   os.path.join(tmp, "sessions")):
                self.assertIsNone(self.module.read_codex_usage())

    def test_fresh_opencodex_cache_beats_rollout_snapshots(self):
        import time

        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            # Rollout says 3% …
            self._rollout(tmp, "rollout-live.jsonl", now - 60, {
                "primary": {"used_percent": 3.0, "window_minutes": 10080,
                            "resets_at": now + 3600}})
            # … but the opencodex service refreshed the quota a minute ago
            # saying 55%. The cache must win.
            cache = pathlib.Path(tmp) / "codex-quota-cache.json"
            cache.write_text(json.dumps({"version": 1, "quotas": {
                "__main__": {"updatedAt": int(time.time() * 1000) - 60_000,
                             "weeklyPercent": 55,
                             "weeklyResetAt": now + 7200}}}), encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": str(cache)}):
                with mock.patch.object(self.module, "CODEX_SESSIONS_DIR",
                                       os.path.join(tmp, "sessions")):
                    usage = self.module.read_codex_usage()
        self.assertEqual(usage["seven_day"], 55)
        self.assertEqual(usage["provider"], "Codex")
        self.assertEqual(usage["level"], "high")

    def test_stale_opencodex_cache_falls_back_to_rollouts(self):
        import time

        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            self._rollout(tmp, "rollout-live.jsonl", now - 60, {
                "primary": {"used_percent": 3.0, "window_minutes": 10080,
                            "resets_at": now + 3600}})
            cache = pathlib.Path(tmp) / "codex-quota-cache.json"
            cache.write_text(json.dumps({"version": 1, "quotas": {
                "__main__": {"updatedAt": int(time.time() * 1000) - 30 * 60_000,
                             "weeklyPercent": 55,
                             "weeklyResetAt": now + 7200}}}), encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": str(cache)}):
                with mock.patch.object(self.module, "CODEX_SESSIONS_DIR",
                                       os.path.join(tmp, "sessions")):
                    usage = self.module.read_codex_usage()
        self.assertEqual(usage["seven_day"], 3)


class OpenCodeGoQuotaTests(unittest.TestCase):
    def setUp(self):
        self.module = load_usage_daemon()

    def _cache(self, tmpdir, quotas):
        path = pathlib.Path(tmpdir) / "codex-quota-cache.json"
        path.write_text(json.dumps({"version": 1, "quotas": quotas}),
                        encoding="utf-8")
        return str(path)

    def test_api_snapshot_maps_all_three_windows(self):
        import time

        now = int(time.time())
        future = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                            time.gmtime(now + secs))
        resp = {"usage": {
            "rolling": {"status": "ok", "percent": 5, "resetsAt": future(3600)},
            "weekly": {"status": "ok", "percent": 2, "resetsAt": future(86400)},
            "monthly": {"status": "ok", "percent": 1, "resetsAt": future(30 * 86400)},
        }}
        snapshot = self.module.opencode_snapshot_from_api(resp, now_epoch=now)
        self.assertEqual(snapshot["provider"], "OpenCode")
        self.assertEqual(snapshot["plan"], "Go")
        self.assertEqual(snapshot["five_hour"], 5)
        self.assertEqual(snapshot["seven_day"], 2)
        self.assertEqual(snapshot["monthly"], 1)
        self.assertEqual(snapshot["level"], "low")
        self.assertTrue(snapshot["five_hour_reset"])

    def test_api_snapshot_level_takes_worst_window(self):
        import time

        future = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                            time.gmtime(time.time() + secs))
        resp = {"usage": {
            "rolling": {"percent": 85, "resetsAt": future(3600)},
            "weekly": {"percent": 2, "resetsAt": future(86400)},
        }}
        snapshot = self.module.opencode_snapshot_from_api(resp)
        self.assertEqual(snapshot["level"], "max")
        self.assertNotIn("monthly", snapshot)

    def test_api_snapshot_rejects_empty_payload(self):
        self.assertIsNone(self.module.opencode_snapshot_from_api({}))
        self.assertIsNone(self.module.opencode_snapshot_from_api(
            {"usage": {"rolling": {"percent": None}}}))

    def _config(self, tmpdir, provider):
        path = pathlib.Path(tmpdir) / "config.json"
        path.write_text(json.dumps({"providers": {"opencode-go": provider}}),
                        encoding="utf-8")
        return str(path)

    def test_api_key_read_from_config_and_pool_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_CONFIG":
                                              self._config(tmp, {"apiKey": "sk-direct"})}):
                self.assertEqual(self.module.opencode_go_api_key(), "sk-direct")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_CONFIG":
                                              self._config(tmp, {"apiKeyPool": [
                                                  {"id": "1", "key": "sk-pool"}]})}):
                self.assertEqual(self.module.opencode_go_api_key(), "sk-pool")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_CONFIG":
                                              self._config(tmp, {"apiKey": ""})}):
                self.assertIsNone(self.module.opencode_go_api_key())

    def test_poll_prefers_api_and_falls_back_to_cache(self):
        import time

        now_ms = int(time.time() * 1000)
        api_resp = {"usage": {
            "rolling": {"percent": 5, "resetsAt": "2026-08-16T14:44:19Z"},
            "weekly": {"percent": 2, "resetsAt": "2026-08-17T00:00:00Z"},
            "monthly": {"percent": 1, "resetsAt": "2026-09-16T02:50:10Z"},
        }}
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, {"__main__": {
                "updatedAt": now_ms - 60_000, "weeklyPercent": 4,
                "weeklyResetAt": int(time.time()) + 3600}})
            env = {"VIBE_ISLAND_OPENCODEX_CONFIG":
                   self._config(tmp, {"apiKey": "sk-x"}),
                   "VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": cache}
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(self.module, "http_get_json",
                                       return_value=api_resp) as get:
                    with mock.patch.object(self.module, "push_usage",
                                           return_value=True) as push:
                        snapshot = self.module.poll_opencode_go()
                get.assert_called_once()
                self.assertEqual(snapshot["seven_day"], 2)
                self.assertEqual(push.call_args[0][0]["five_hour"], 5)

                # API unreachable → cache fallback (weekly 4%).
                with mock.patch.object(
                        self.module, "http_get_json",
                        side_effect=OSError("down")):
                    with mock.patch.object(self.module, "push_usage",
                                           return_value=True):
                        snapshot = self.module.poll_opencode_go()
                self.assertEqual(snapshot["seven_day"], 4)
                self.assertNotIn("five_hour", snapshot)

    def test_reads_weekly_percent_and_reset(self):
        import time

        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, {"__main__": {
                "updatedAt": now_ms - 60_000,
                "weeklyPercent": 4,
                "weeklyResetAt": int(time.time()) + 3600,
                "resetCredits": 0}})
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": cache}):
                snapshot = self.module.read_opencode_quota_cache()
        self.assertEqual(snapshot["provider"], "OpenCode")
        self.assertEqual(snapshot["plan"], "Go")
        self.assertEqual(snapshot["seven_day"], 4)
        self.assertIn("seven_day_reset", snapshot)
        self.assertNotIn("credits", snapshot)

    def test_credits_only_when_no_weekly_percent(self):
        import time

        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, {"go": {
                "updatedAt": now_ms - 60_000,
                "weeklyPercent": None,
                "resetCredits": 1250}})
            with mock.patch.dict(os.environ,
                                 {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE": cache}):
                snapshot = self.module.read_opencode_quota_cache()
        self.assertEqual(snapshot["credits"], "1,250")
        self.assertNotIn("seven_day", snapshot)

    def test_picks_newest_entry_and_skips_stale_cache(self):
        import time

        now_ms = int(time.time() * 1000)
        stale = {"old": {"updatedAt": now_ms - 8 * 86400 * 1000,
                         "weeklyPercent": 90, "weeklyResetAt": now_ms // 1000}}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE":
                                              self._cache(tmp, stale)}):
                self.assertIsNone(self.module.read_opencode_quota_cache())
        fresh = {
            "a": {"updatedAt": now_ms - 3_600_000, "weeklyPercent": 1,
                  "weeklyResetAt": int(time.time()) + 7200},
            "b": {"updatedAt": now_ms - 60_000, "weeklyPercent": 7,
                  "weeklyResetAt": int(time.time()) + 7200},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE":
                                              self._cache(tmp, fresh)}):
                snapshot = self.module.read_opencode_quota_cache()
        self.assertEqual(snapshot["seven_day"], 7)

    def test_missing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VIBE_ISLAND_OPENCODEX_QUOTA_CACHE":
                                              os.path.join(tmp, "none.json")}):
                self.assertIsNone(self.module.read_opencode_quota_cache())


if __name__ == "__main__":
    unittest.main()
