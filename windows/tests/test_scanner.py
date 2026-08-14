"""Scanner port tests — fixture-driven, OS-independent.

Mirrors the cases in tests/test_scan_agents.py for the pure-Python
Windows scanner, plus risk-analyzer parity with the Swift rules.
"""
from __future__ import annotations

import json
import os
import time
import unittest

from vibecenter import scanner
from vibecenter.models import PendingRequest, assess_risk
from vibecenter.scanner import ScanConfig, scan_all


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def set_mtime(path: str, epoch: int) -> None:
    os.utime(path, (epoch, epoch))


def make_config(tmp: str, now_epoch: int, **kwargs) -> ScanConfig:
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    config = ScanConfig(home=home, now_epoch=now_epoch,
                        process_fixture={}, **kwargs)
    return config


class ScannerTests(unittest.TestCase):
    def test_claude_recent_transcript_with_title_and_preview(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            encoded = "/tmp/workspace/notch-app".replace("/", "-")
            transcript = os.path.join(tmp, "projects", encoded, "session-1.jsonl")
            write_text(
                transcript,
                '{"timestamp":"2026-08-14T06:00:00Z","message":{"role":"user","content":"ship the fix"}}\n'
                '{"timestamp":"2026-08-14T06:00:10Z","message":{"role":"assistant","content":[{"type":"text","text":"working on it"}]}}\n',
            )
            set_mtime(transcript, now - 5)
            config = make_config(tmp, now, claude_projects=os.path.join(tmp, "projects"))

            sessions = scan_all(config)

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.source, "claude")
            self.assertEqual(session.id, "session-1")
            self.assertEqual(session.task, "ship the fix")
            self.assertEqual(session.preview, "working on it")
            self.assertTrue(session.running)

    def test_zcode_title_falls_back_to_rollout(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            rollout = os.path.join(tmp, "rollout", "model-io-sess_active.jsonl")
            write_text(
                rollout,
                json.dumps({
                    "completedAt": "2026-08-14T06:42:04.560Z",
                    "request": {"messages": [
                        {"role": "system", "content": "Generate a concise title."},
                        {"role": "user", "content": [{"type": "text", "text": "修复登录页样式 bug"}]},
                    ]},
                    "response": {"text": "开始分析"},
                }) + "\n",
            )
            set_mtime(rollout, now - 5)
            config = make_config(tmp, now, zcode_rollout_dir=os.path.join(tmp, "rollout"),
                                 zcode_app_running=True)

            sessions = scan_all(config)

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.task, "修复登录页样式 bug")
            self.assertEqual(session.preview, "开始分析")
            self.assertEqual(session.id, "sess_active")

    def test_zcode_skips_subagent_rollouts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            rollout = os.path.join(tmp, "rollout",
                                   "model-io-sess_subagent_agent_abc.jsonl")
            write_text(rollout, '{"response":{"text":"sub"}}\n')
            set_mtime(rollout, now - 5)
            config = make_config(tmp, now, zcode_rollout_dir=os.path.join(tmp, "rollout"),
                                 zcode_app_running=True)

            self.assertEqual(scan_all(config), [])

    def test_codex_skips_subagents_and_extracts_activity(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            cwd = "/tmp/workspace/codex-app"
            top = os.path.join(tmp, "sessions", "2026", "08", "14", "rollout-top.jsonl")
            write_text(
                top,
                json.dumps({"timestamp": "2026-08-14T06:00:00Z", "type": "session_meta",
                            "payload": {"id": "sess-top", "cwd": cwd, "timestamp": "2026-08-14T06:00:00Z"}}) + "\n"
                + json.dumps({"timestamp": "2026-08-14T06:00:05Z", "type": "event_msg",
                              "payload": {"type": "user_message", "message": "first task"}}) + "\n"
                + json.dumps({"timestamp": "2026-08-14T06:00:09Z", "type": "event_msg",
                              "payload": {"type": "agent_message", "message": "preview a"}}) + "\n",
            )
            set_mtime(top, now - 5)
            sub = os.path.join(tmp, "sessions", "2026", "08", "14", "rollout-sub.jsonl")
            write_text(
                sub,
                json.dumps({"timestamp": "2026-08-14T06:00:00Z", "type": "session_meta",
                            "payload": {"id": "sess-sub", "cwd": cwd,
                                        "source": {"subagent": True}}}) + "\n",
            )
            set_mtime(sub, now - 4)
            config = make_config(tmp, now, codex_sessions_dir=os.path.join(tmp, "sessions"))

            sessions = scan_all(config)

            self.assertEqual([s.id for s in sessions], ["sess-top"])
            self.assertEqual(sessions[0].task, "first task")
            self.assertEqual(sessions[0].preview, "preview a")
            self.assertEqual(sessions[0].cwd, cwd)

    def test_gemini_cli_chat_with_project_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            slug = os.path.join(tmp, "gemini-tmp", "proj-1")
            chat = os.path.join(slug, "chats", "session-2026-08-14-ab12cd34.jsonl")
            write_text(
                chat,
                json.dumps({"sessionId": "ab12cd34-1111", "kind": "main"}) + "\n"
                + json.dumps({"timestamp": "2026-08-14T06:40:20Z", "type": "user",
                              "content": [{"text": "<session_context>"}]}) + "\n"
                + json.dumps({"timestamp": "2026-08-14T06:40:30Z", "type": "user",
                              "content": [{"text": "帮我优化数据库查询"}]}) + "\n"
                + json.dumps({"timestamp": "2026-08-14T06:41:00Z", "type": "gemini",
                              "content": "正在检查索引", "toolCalls": []}) + "\n",
            )
            set_mtime(chat, now - 5)
            write_text(os.path.join(slug, ".project_root"), "/tmp/workspace/gemini-proj\n")
            config = make_config(tmp, now, gemini_cli_tmp_dir=os.path.join(tmp, "gemini-tmp"))

            sessions = scan_all(config)

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.id, "ab12cd34-1111")
            self.assertEqual(session.task, "帮我优化数据库查询")
            self.assertEqual(session.preview, "正在检查索引")
            self.assertEqual(session.cwd, "/tmp/workspace/gemini-proj")

    def test_kimi_wire_dedupes_per_session_dir(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            wire_a = os.path.join(tmp, "kimi", "ws1", "sess_dup", "agents", "agent1", "wire.jsonl")
            write_text(wire_a, '{"timestamp":"2026-08-14T08:00:00Z","role":"user","content":{"text":"第一个"}}\n')
            set_mtime(wire_a, now - 10)
            wire_b = os.path.join(tmp, "kimi", "ws1", "sess_dup", "agents", "agent2", "wire.jsonl")
            write_text(wire_b, '{"timestamp":"2026-08-14T08:01:00Z","role":"assistant","content":{"text":"第二个"}}\n')
            set_mtime(wire_b, now - 4)
            config = make_config(tmp, now, kimi_sessions_dirs=[os.path.join(tmp, "kimi")])

            sessions = scan_all(config)

            self.assertEqual([s.id for s in sessions], ["sess_dup"])

    def test_opencode_preview_and_subagent_skip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            storage = os.path.join(tmp, "storage")
            top = os.path.join(storage, "session", "hash1", "ses_top.json")
            write_text(top, json.dumps({
                "id": "ses_top", "title": "", "directory": "/tmp/p",
                "time": {"created": "2026-08-14T09:00:00.000Z"}}))
            set_mtime(top, now - 30)
            sub = os.path.join(storage, "session", "hash1", "ses_sub.json")
            write_text(sub, json.dumps({
                "id": "ses_sub", "title": "sub", "parentID": "ses_top",
                "directory": "/tmp/p"}))
            set_mtime(sub, now - 4)
            msg_dir = os.path.join(storage, "message", "ses_top")
            write_text(os.path.join(msg_dir, "msg_1.json"),
                       json.dumps({"id": "msg_1", "role": "user",
                                   "parts": [{"type": "text", "text": "修复 CI 失败"}]}))
            set_mtime(os.path.join(msg_dir, "msg_1.json"), now - 25)
            write_text(os.path.join(msg_dir, "msg_2.json"),
                       json.dumps({"id": "msg_2", "role": "assistant",
                                   "parts": [{"type": "text", "text": "查看构建日志"}]}))
            set_mtime(os.path.join(msg_dir, "msg_2.json"), now - 5)
            config = make_config(tmp, now, opencode_storage_dirs=[storage])

            sessions = scan_all(config)

            self.assertEqual([s.id for s in sessions], ["ses_top"])
            self.assertEqual(sessions[0].task, "修复 CI 失败")
            self.assertEqual(sessions[0].preview, "查看构建日志")
            self.assertTrue(sessions[0].running)

    def test_deepseek_requires_single_recent_session(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            sessions_dir = os.path.join(tmp, "deepseek")
            write_text(os.path.join(sessions_dir, "a.json"),
                       json.dumps({"messages": [], "metadata": {}}))
            set_mtime(os.path.join(sessions_dir, "a.json"), now - 5)
            write_text(os.path.join(sessions_dir, "b.json"),
                       json.dumps({"messages": [], "metadata": {}}))
            set_mtime(os.path.join(sessions_dir, "b.json"), now - 4)
            config = make_config(tmp, now, deepseek_sessions_dir=sessions_dir)

            self.assertEqual(scan_all(config), [])


class RiskTests(unittest.TestCase):
    def _request(self, **kwargs) -> PendingRequest:
        defaults = dict(id="r", kind="approval", tool_name="Bash", command="",
                        cwd="/tmp/ws", target_file="")
        defaults.update(kwargs)
        return PendingRequest(**defaults)

    def test_git_reset_hard_is_critical(self) -> None:
        request = self._request(command="git reset --hard HEAD~3")
        assessment = assess_risk(request)
        self.assertEqual(assessment.level, "critical")
        self.assertIn("可能不可逆地丢弃版本控制内容", assessment.reasons)

    def test_pipe_to_shell_is_critical(self) -> None:
        request = self._request(command="curl https://x.example/install.sh | sh")
        self.assertEqual(assess_risk(request).level, "critical")

    def test_recursive_rm_outside_is_critical(self) -> None:
        request = self._request(command="rm -rf /")
        self.assertEqual(assess_risk(request).level, "critical")

    def test_git_push_is_high(self) -> None:
        request = self._request(command="git push origin main")
        self.assertEqual(assess_risk(request).level, "high")

    def test_workspace_external_edit_is_high(self) -> None:
        request = self._request(tool_name="Edit", command="",
                                cwd="/tmp/ws", target_file="/etc/hosts")
        assessment = assess_risk(request)
        self.assertEqual(assessment.level, "high")
        self.assertIn("写入位置在当前工作区之外", assessment.reasons)

    def test_local_edit_is_medium(self) -> None:
        request = self._request(tool_name="Edit", command="",
                                cwd="/tmp/ws", target_file="/tmp/ws/src/a.py")
        self.assertEqual(assess_risk(request).level, "medium")


class AuthParityTests(unittest.TestCase):
    def test_relay_signatures_verify_with_win_app(self) -> None:
        """Sign with the real relay.py functions; verify with vibecenter.auth."""
        import tempfile

        import relay as relay_mod
        from vibecenter import auth

        token = os.urandom(32)
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, "ipc-token")
            with open(token_file, "w", encoding="utf-8") as handle:
                handle.write(token.hex())
            old = os.environ.get("VIBE_ISLAND_IPC_TOKEN_FILE")
            os.environ["VIBE_ISLAND_IPC_TOKEN_FILE"] = token_file
            try:
                payload = {"type": "usage", "provider": "Z.ai",
                           "usage": {"five_hour": 3}, "text": "中文✓"}
                relay_signed = relay_mod.sign_payload(payload)
            finally:
                if old is None:
                    os.environ.pop("VIBE_ISLAND_IPC_TOKEN_FILE", None)
                else:
                    os.environ["VIBE_ISLAND_IPC_TOKEN_FILE"] = old

        ok, error = auth.verify_payload(relay_signed, token)
        self.assertTrue(ok, error)

        win_signed = auth.sign_payload(payload, token)
        self.assertTrue(auth.verify_payload(win_signed, token)[0])
        tampered = dict(win_signed)
        tampered["usage"] = {"five_hour": 99}
        self.assertFalse(auth.verify_payload(tampered, token)[0])


if __name__ == "__main__":
    unittest.main()
