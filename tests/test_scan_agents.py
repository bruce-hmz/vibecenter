import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path("/Volumes/RTL9210/workspace/claude-projects/vibe-island-app")
SCAN_SCRIPT = ROOT / "scan-agents.sh"


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def set_mtime(path: pathlib.Path, epoch: int) -> None:
    os.utime(path, (epoch, epoch))


class ScanAgentsTests(unittest.TestCase):
    def run_scan(self, env: dict[str, str]) -> list[dict]:
        result = subprocess.run(
            ["zsh", str(SCAN_SCRIPT)],
            cwd=ROOT,
            env={**os.environ, **env},
            check=True,
            capture_output=True,
            text=True,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_claude_detail_uses_full_cwd_and_emits_short_display_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            cwd = "/tmp/workspace/notch-app"
            encoded = cwd.replace("/", "-")
            transcript = tmpdir / "claude-projects" / encoded / "session-1.jsonl"
            write_text(
                transcript,
                textwrap.dedent(
                    """
                    {"timestamp":"2026-07-30T12:00:00Z","message":{"role":"user","content":"ship the fix"}}
                    {"timestamp":"2026-07-30T12:00:10Z","message":{"role":"assistant","content":[{"type":"text","text":"working on it"}]}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript, now_epoch - 5)

            fixture = tmpdir / "claude-fixture.tsv"
            write_text(fixture, f"123\t{cwd}\tclaude\t{now_epoch - 20}\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "claude",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_CLAUDE_PROJECTS": str(tmpdir / "claude-projects"),
                    "VIBE_ISLAND_CLAUDE_PROCESS_FIXTURE": str(fixture),
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["source"], "claude")
            self.assertEqual(session["detail"], cwd)
            self.assertEqual(session["cwd"], cwd)
            self.assertEqual(session["display_detail"], "notch-app")
            self.assertEqual(session["transcript_path"], str(transcript))
            self.assertEqual(session["match_confidence"], "cwd")

    def test_codex_uses_unique_transcript_per_pid_in_same_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_785_412_920
            cwd = "/tmp/workspace/vibe-island-app"
            sessions_dir = tmpdir / "codex-sessions" / "2026" / "07" / "30"

            transcript_a = sessions_dir / "rollout-1.jsonl"
            write_text(
                transcript_a,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:00:00Z","type":"session_meta","payload":{{"id":"sess-a","timestamp":"2026-07-30T12:00:00Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:00:20Z","type":"event_msg","payload":{{"type":"user_message","message":"first task"}}}}
                    {{"timestamp":"2026-07-30T12:00:25Z","type":"event_msg","payload":{{"type":"agent_message","message":"preview a"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript_a, now_epoch - 8)

            transcript_b = sessions_dir / "rollout-2.jsonl"
            write_text(
                transcript_b,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:01:00Z","type":"session_meta","payload":{{"id":"sess-b","timestamp":"2026-07-30T12:01:00Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:01:10Z","type":"event_msg","payload":{{"type":"user_message","message":"second task"}}}}
                    {{"timestamp":"2026-07-30T12:01:15Z","type":"event_msg","payload":{{"type":"agent_message","message":"preview b"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript_b, now_epoch - 4)

            fixture = tmpdir / "codex-fixture.tsv"
            write_text(
                fixture,
                "\n".join(
                    [
                        f"501\t{cwd}\t/opt/codex\t1785412785",
                        f"502\t{cwd}\t/opt/codex\t1785412895",
                    ]
                )
                + "\n",
            )

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "codex",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_CODEX_SESSIONS_DIR": str(tmpdir / "codex-sessions"),
                    "VIBE_ISLAND_CODEX_PROCESS_FIXTURE": str(fixture),
                }
            )

            self.assertEqual(len(sessions), 2)
            by_id = {session["session_id"]: session for session in sessions}
            self.assertEqual(by_id["sess-a"]["transcript_path"], str(transcript_a))
            self.assertEqual(by_id["sess-b"]["transcript_path"], str(transcript_b))
            self.assertNotEqual(
                by_id["sess-a"]["transcript_path"],
                by_id["sess-b"]["transcript_path"],
            )
            self.assertEqual(by_id["sess-a"]["match_confidence"], "start_window")
            self.assertEqual(by_id["sess-b"]["match_confidence"], "start_time")
            self.assertEqual(by_id["sess-a"]["pid"], "501")
            self.assertEqual(by_id["sess-b"]["pid"], "502")

    def test_codex_skips_transcript_without_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_785_412_920
            cwd = "/tmp/workspace/vibe-island-app"
            sessions_dir = tmpdir / "codex-sessions" / "2026" / "07" / "30"

            transcript = sessions_dir / "rollout-missing-id.jsonl"
            write_text(
                transcript,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:00:00Z","type":"session_meta","payload":{{"id":"","timestamp":"2026-07-30T12:00:00Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:00:20Z","type":"event_msg","payload":{{"type":"agent_message","message":"preview a"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript, now_epoch - 8)

            fixture = tmpdir / "codex-fixture.tsv"
            write_text(fixture, f"501\t{cwd}\t/opt/codex\t1785412785\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "codex",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_CODEX_SESSIONS_DIR": str(tmpdir / "codex-sessions"),
                    "VIBE_ISLAND_CODEX_PROCESS_FIXTURE": str(fixture),
                }
            )

            self.assertEqual(sessions, [])

    def test_codex_ambiguous_pid_match_falls_back_to_rollout_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_785_412_920
            cwd = "/tmp/workspace/vibe-island-app"
            sessions_dir = tmpdir / "codex-sessions" / "2026" / "07" / "30"

            transcript_a = sessions_dir / "rollout-a.jsonl"
            write_text(
                transcript_a,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:00:00Z","type":"session_meta","payload":{{"id":"sess-a","timestamp":"2026-07-30T12:00:00Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:00:02Z","type":"event_msg","payload":{{"type":"agent_message","message":"preview a"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript_a, now_epoch - 5)

            transcript_b = sessions_dir / "rollout-b.jsonl"
            write_text(
                transcript_b,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:00:03Z","type":"session_meta","payload":{{"id":"sess-b","timestamp":"2026-07-30T12:00:03Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:00:04Z","type":"event_msg","payload":{{"type":"agent_message","message":"preview b"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript_b, now_epoch - 4)

            fixture = tmpdir / "codex-fixture.tsv"
            write_text(fixture, f"601\t{cwd}\t/opt/codex\t1785412801\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "codex",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_CODEX_SESSIONS_DIR": str(tmpdir / "codex-sessions"),
                    "VIBE_ISLAND_CODEX_PROCESS_FIXTURE": str(fixture),
                }
            )

            # The PID scan cannot uniquely match either transcript (timestamps
            # are too close), so it produces no start-time-matched session.
            # The rollout fallback scan picks up both top-level sessions
            # because they are active regardless of PID association.
            confidences = [s.get("match_confidence", "") for s in sessions]
            self.assertNotIn("start_window", confidences)
            self.assertNotIn("start_time", confidences)
            ids = sorted(s["session_id"] for s in sessions)
            self.assertEqual(ids, ["sess-a", "sess-b"])
            self.assertTrue(all(c == "rollout_scan" for c in confidences))

    def test_codex_rollout_scan_finds_desktop_sessions_without_pid(self) -> None:
        # Simulates ChatGPT desktop App: no /codex CLI process exists, but
        # the session rollout file is actively being written. The rollout
        # fallback scan must surface it.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_785_412_920
            cwd = "/tmp/workspace/vibe-island-app"
            sessions_dir = tmpdir / "codex-sessions" / "2026" / "07" / "30"

            transcript = sessions_dir / "rollout-desktop.jsonl"
            write_text(
                transcript,
                textwrap.dedent(
                    f"""
                    {{"timestamp":"2026-07-30T12:00:00Z","type":"session_meta","payload":{{"id":"desktop-sess","timestamp":"2026-07-30T12:00:00Z","cwd":"{cwd}","source":"vscode"}}}}
                    {{"timestamp":"2026-07-30T12:00:10Z","type":"event_msg","payload":{{"type":"agent_message","message":"working on fix"}}}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(transcript, now_epoch - 3)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "codex",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_CODEX_SESSIONS_DIR": str(tmpdir / "codex-sessions"),
                    # No process fixture — simulates desktop App with no /codex PID.
                }
            )

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], "desktop-sess")
            self.assertEqual(sessions[0]["source"], "codex")
            self.assertEqual(sessions[0]["match_confidence"], "rollout_scan")
            self.assertTrue(sessions[0]["running"])

    def test_zcode_skips_stale_rollout_even_when_app_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            rollout_dir = tmpdir / "zcode-rollout"
            stale_rollout = rollout_dir / "model-io-sess_stale.jsonl"
            write_text(stale_rollout, '{"completedAt":"2026-07-30T12:00:00Z","response":{"text":"stale"}}\n')
            set_mtime(stale_rollout, now_epoch - 3_600)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "zcode",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_ZCODE_APP_RUNNING": "true",
                    "VIBE_ISLAND_ZCODE_ROLLOUT_DIR": str(rollout_dir),
                    "VIBE_ISLAND_ZCODE_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(sessions, [])

    def test_gemini_emits_only_single_recent_log_for_single_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            cwd = "/tmp/workspace/gemini-app"
            log_dir = tmpdir / "gemini-log"
            log_file = log_dir / "cli-1.log"
            write_text(
                log_file,
                textwrap.dedent(
                    """
                    I0730 12:00:00.000 Sending user message to conversation abcdef12-3456-7890-abcd-ef1234567890
                    I0730 12:00:10.000 step finished
                    """
                ).strip()
                + "\n",
            )
            set_mtime(log_file, now_epoch - 5)

            fixture = tmpdir / "gemini-fixture.tsv"
            write_text(fixture, f"701\t{cwd}\t/opt/agy\t{now_epoch - 20}\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "gemini",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_GEMINI_LOG_DIR": str(log_dir),
                    "VIBE_ISLAND_GEMINI_PROCESS_FIXTURE": str(fixture),
                    "VIBE_ISLAND_GEMINI_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["session_id"], "gemini-701")
            self.assertEqual(session["source"], "gemini")
            self.assertEqual(session["detail"], "gemini-app")
            self.assertEqual(session["cwd"], cwd)
            self.assertEqual(session["transcript_path"], str(log_file))
            self.assertEqual(session["match_confidence"], "single_recent_log")

    def test_gemini_skips_ambiguous_multiple_pids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            log_dir = tmpdir / "gemini-log"
            log_file = log_dir / "cli-1.log"
            write_text(log_file, "I0730 12:00:00.000 Sending user message to conversation abcdef12-3456-7890-abcd-ef1234567890\n")
            set_mtime(log_file, now_epoch - 5)

            fixture = tmpdir / "gemini-fixture.tsv"
            write_text(
                fixture,
                "\n".join(
                    [
                        "701\t/tmp/workspace/a\t/opt/agy\t1799999980",
                        "702\t/tmp/workspace/b\t/opt/agy\t1799999985",
                    ]
                )
                + "\n",
            )

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "gemini",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_GEMINI_LOG_DIR": str(log_dir),
                    "VIBE_ISLAND_GEMINI_PROCESS_FIXTURE": str(fixture),
                    "VIBE_ISLAND_GEMINI_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(sessions, [])

    def test_deepseek_emits_only_single_recent_session_for_single_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            cwd = "/tmp/workspace/deepseek-app"
            sessions_dir = tmpdir / "deepseek-sessions"
            session_file = sessions_dir / "session-1.json"
            write_text(
                session_file,
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": "fix the build"},
                            {"role": "assistant", "content": "checking logs"},
                        ],
                        "metadata": {"updatedAt": "2026-07-30T12:00:00Z"},
                    }
                ),
            )
            set_mtime(session_file, now_epoch - 6)

            fixture = tmpdir / "deepseek-fixture.tsv"
            write_text(fixture, f"801\t{cwd}\t/opt/deepseek\t{now_epoch - 30}\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "deepseek",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_DEEPSEEK_SESSIONS_DIR": str(sessions_dir),
                    "VIBE_ISLAND_DEEPSEEK_PROCESS_FIXTURE": str(fixture),
                    "VIBE_ISLAND_DEEPSEEK_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["session_id"], "deepseek-801")
            self.assertEqual(session["detail"], "deepseek-app")
            self.assertEqual(session["cwd"], cwd)
            self.assertEqual(session["transcript_path"], str(session_file))
            self.assertEqual(session["match_confidence"], "single_recent_session")

    def test_deepseek_skips_ambiguous_multiple_recent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            sessions_dir = tmpdir / "deepseek-sessions"

            session_a = sessions_dir / "session-a.json"
            write_text(session_a, json.dumps({"messages": [], "metadata": {"updatedAt": "2026-07-30T12:00:00Z"}}))
            set_mtime(session_a, now_epoch - 5)

            session_b = sessions_dir / "session-b.json"
            write_text(session_b, json.dumps({"messages": [], "metadata": {"updatedAt": "2026-07-30T12:00:01Z"}}))
            set_mtime(session_b, now_epoch - 4)

            fixture = tmpdir / "deepseek-fixture.tsv"
            write_text(fixture, "801\t/tmp/workspace/deepseek\t/opt/deepseek\t1799999970\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "deepseek",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_DEEPSEEK_SESSIONS_DIR": str(sessions_dir),
                    "VIBE_ISLAND_DEEPSEEK_PROCESS_FIXTURE": str(fixture),
                    "VIBE_ISLAND_DEEPSEEK_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(sessions, [])


if __name__ == "__main__":
    unittest.main()
