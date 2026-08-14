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
        self.last_stderr = result.stderr
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

    def test_claude_project_dir_without_transcripts_is_silent(self) -> None:
        # Regression: an existing project dir with no .jsonl transcripts used
        # to leak zsh's "no matches found" error to stderr.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            cwd = "/tmp/workspace/empty-project"
            encoded = cwd.replace("/", "-")
            (tmpdir / "claude-projects" / encoded).mkdir(parents=True)

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

            self.assertEqual(sessions, [])
            self.assertNotIn("no matches found", self.last_stderr)

    def test_zcode_title_falls_back_to_rollout_first_user_message(self) -> None:
        # Newer ZCode versions don't always create ~/.zcode/cli/agents/<sess>
        # for main sessions; the title must come from the rollout's first
        # user prompt instead of degrading to "ZCode".
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            rollout_dir = tmpdir / "zcode-rollout"
            rollout = rollout_dir / "model-io-sess_active.jsonl"
            write_text(
                rollout,
                textwrap.dedent(
                    """
                    {"startedAt":"2026-08-14T06:42:01.000Z","completedAt":"2026-08-14T06:42:04.560Z","request":{"messages":[{"role":"system","content":"Generate a concise title for this coding session."},{"role":"user","content":[{"type":"text","text":"修复登录页面的样式 bug"}]}]},"response":{"text":"开始分析"}}
                    {"startedAt":"2026-08-14T06:43:00.000Z","completedAt":"2026-08-14T06:43:05.000Z","request":{"messages":[{"role":"user","content":"<file_changed>/tmp/x</file_changed>"}]},"response":{"toolCalls":[{"toolName":"Read"}]}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(rollout, now_epoch - 5)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "zcode",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_ZCODE_APP_RUNNING": "true",
                    "VIBE_ISLAND_ZCODE_ROLLOUT_DIR": str(rollout_dir),
                    "VIBE_ISLAND_ZCODE_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["source"], "zcode")
            self.assertEqual(session["task"], "修复登录页面的样式 bug")
            self.assertEqual(session["preview"], "开始分析")

    def test_gemini_cli_emits_recent_chat_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            cwd = "/tmp/workspace/gemini-cli-project"
            slug = tmpdir / "gemini-tmp" / "gemini-cli-project-1"
            chat = slug / "chats" / "session-2026-08-14T06-40-ab12cd34.jsonl"
            write_text(
                chat,
                textwrap.dedent(
                    """
                    {"sessionId":"ab12cd34-1111-2222-3333-444455556666","startTime":"2026-08-14T06:40:00.000Z","lastUpdated":"2026-08-14T06:41:00.000Z","kind":"main"}
                    {"id":"m1","timestamp":"2026-08-14T06:40:10.000Z","type":"user","content":[{"text":"<session_context>\\nsetup"}]}
                    {"id":"m2","timestamp":"2026-08-14T06:40:20.000Z","type":"user","content":[{"text":"帮我优化数据库查询"}]}
                    {"id":"m3","timestamp":"2026-08-14T06:41:00.000Z","type":"gemini","content":"正在检查索引","toolCalls":[]}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(chat, now_epoch - 5)
            write_text(slug / ".project_root", cwd + "\n")

            fixture = tmpdir / "gemini-cli-fixture.tsv"
            write_text(fixture, f"901\t{cwd}\t/opt/gemini\t{now_epoch - 60}\n")

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "gemini",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_GEMINI_CLI_TMP_DIR": str(tmpdir / "gemini-tmp"),
                    "VIBE_ISLAND_GEMINI_CLI_PROCESS_FIXTURE": str(fixture),
                    "VIBE_ISLAND_GEMINI_CLI_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["source"], "gemini")
            self.assertEqual(session["session_id"], "ab12cd34-1111-2222-3333-444455556666")
            self.assertEqual(session["task"], "帮我优化数据库查询")
            self.assertEqual(session["preview"], "正在检查索引")
            self.assertEqual(session["cwd"], cwd)
            self.assertEqual(session["transcript_path"], str(chat))
            self.assertEqual(session["pid"], "901")
            self.assertTrue(session["running"])

    def test_qwen_uses_same_chat_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            slug = tmpdir / "qwen-tmp" / "qwen-project"
            chat = slug / "chats" / "session-2026-08-14T07-00-99aabbcc.jsonl"
            write_text(
                chat,
                textwrap.dedent(
                    """
                    {"sessionId":"99aabbcc-aaaa-bbbb-cccc-ddddeeeeffff","startTime":"2026-08-14T07:00:00.000Z","kind":"main"}
                    {"id":"m1","timestamp":"2026-08-14T07:00:05.000Z","type":"user","content":[{"text":"写一个排序算法"}]}
                    {"id":"m2","timestamp":"2026-08-14T07:00:30.000Z","type":"gemini","content":"好的，这是快速排序"}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(chat, now_epoch - 5)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "qwen",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_QWEN_TMP_DIR": str(tmpdir / "qwen-tmp"),
                    "VIBE_ISLAND_QWEN_ACTIVE_WINDOW_SECS": "300",
                    # Empty fixture: no qwen process exists, so no pid is
                    # attached (avoids matching ambient processes on the
                    # machine running the tests).
                    "VIBE_ISLAND_QWEN_PROCESS_FIXTURE": str(tmpdir / "empty.tsv"),
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["source"], "qwen")
            self.assertEqual(session["task"], "写一个排序算法")
            self.assertEqual(session["preview"], "好的，这是快速排序")
            self.assertEqual(session["pid"], "")

    def test_kimi_emits_recent_wire_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            sessions_dir = tmpdir / "kimi-sessions"
            wire = sessions_dir / "grp1" / "sess_abc123" / "wire.jsonl"
            write_text(
                wire,
                textwrap.dedent(
                    """
                    {"timestamp":"2026-08-14T08:00:00Z","role":"user","content":{"text":"重构配置模块"}}
                    {"timestamp":"2026-08-14T08:00:30Z","role":"assistant","content":{"text":"先看现有结构"}}
                    """
                ).strip()
                + "\n",
            )
            set_mtime(wire, now_epoch - 5)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "kimi",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_KIMI_SESSIONS_DIR": str(sessions_dir),
                    "VIBE_ISLAND_KIMI_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["source"], "kimi")
            self.assertEqual(session["session_id"], "sess_abc123")
            self.assertEqual(session["task"], "重构配置模块")
            self.assertEqual(session["preview"], "先看现有结构")
            self.assertEqual(session["transcript_path"], str(wire))

    def test_kimi_dedupes_multiple_wires_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            sessions_dir = tmpdir / "kimi-sessions"
            # kimi-code layout: <ws>/<session>/agents/<agent>/wire.jsonl
            wire_a = sessions_dir / "ws1" / "sess_dup" / "agents" / "agent1" / "wire.jsonl"
            write_text(wire_a, '{"timestamp":"2026-08-14T08:00:00Z","role":"user","content":{"text":"第一个"}}\n')
            set_mtime(wire_a, now_epoch - 10)
            wire_b = sessions_dir / "ws1" / "sess_dup" / "agents" / "agent2" / "wire.jsonl"
            write_text(wire_b, '{"timestamp":"2026-08-14T08:01:00Z","role":"assistant","content":{"text":"第二个"}}\n')
            set_mtime(wire_b, now_epoch - 4)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "kimi",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_KIMI_SESSIONS_DIR": str(sessions_dir),
                    "VIBE_ISLAND_KIMI_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], "sess_dup")

    def test_opencode_emits_recent_session_with_message_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            storage = tmpdir / "opencode-storage"
            cwd = "/tmp/workspace/opencode-project"
            session = storage / "session" / "hash1" / "ses_op1.json"
            write_text(session, json.dumps({
                "id": "ses_op1",
                "title": "",
                "directory": cwd,
                "time": {"created": "2026-08-14T09:00:00.000Z"},
            }))
            set_mtime(session, now_epoch - 30)
            msg_dir = storage / "message" / "ses_op1"
            user_msg = msg_dir / "msg_001_u.json"
            write_text(user_msg, json.dumps({"id": "msg_001_u", "role": "user", "parts": [{"type": "text", "text": "修复 CI 失败"}]}))
            set_mtime(user_msg, now_epoch - 25)
            assistant_msg = msg_dir / "msg_002_a.json"
            write_text(assistant_msg, json.dumps({"id": "msg_002_a", "role": "assistant", "parts": [{"type": "text", "text": "查看构建日志"}]}))
            set_mtime(assistant_msg, now_epoch - 5)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "opencode",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_OPENCODE_STORAGE_DIR": str(storage),
                    "VIBE_ISLAND_OPENCODE_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual(len(sessions), 1)
            found = sessions[0]
            self.assertEqual(found["source"], "opencode")
            self.assertEqual(found["session_id"], "ses_op1")
            self.assertEqual(found["task"], "修复 CI 失败")
            self.assertEqual(found["preview"], "查看构建日志")
            self.assertEqual(found["cwd"], cwd)
            # The newest message file was written 5s ago → running.
            self.assertTrue(found["running"])

    def test_opencode_skips_subagent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            now_epoch = 1_800_000_000
            storage = tmpdir / "opencode-storage"
            top = storage / "session" / "hash1" / "ses_top.json"
            write_text(top, json.dumps({"id": "ses_top", "title": "top", "directory": "/tmp/p", "time": {"created": "2026-08-14T09:00:00.000Z"}}))
            set_mtime(top, now_epoch - 5)
            sub = storage / "session" / "hash1" / "ses_sub.json"
            write_text(sub, json.dumps({"id": "ses_sub", "title": "sub", "parentID": "ses_top", "directory": "/tmp/p", "time": {"created": "2026-08-14T09:00:10.000Z"}}))
            set_mtime(sub, now_epoch - 4)

            sessions = self.run_scan(
                {
                    "VIBE_ISLAND_ONLY_SOURCES": "opencode",
                    "VIBE_ISLAND_NOW_EPOCH": str(now_epoch),
                    "VIBE_ISLAND_OPENCODE_STORAGE_DIR": str(storage),
                    "VIBE_ISLAND_OPENCODE_ACTIVE_WINDOW_SECS": "300",
                }
            )

            self.assertEqual([s["session_id"] for s in sessions], ["ses_top"])


if __name__ == "__main__":
    unittest.main()
