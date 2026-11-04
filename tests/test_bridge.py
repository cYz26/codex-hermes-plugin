import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.normalize import merge_event, observation
from hermes_codex_learning.store import Store, atomic_json, redact


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "home", self.root / "project", self.root / "source", Path("/usr/bin/python3"))
        self.settings.project.mkdir()
        self.store = Store(self.settings.state)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, kind, **fields):
        return {"hook_event_name": kind, "cwd": str(self.settings.project), "session_id": "chat-a", "turn_id": "turn-a", **fields}

    def merge(self, payload):
        identity = self.store.capture(payload)
        row = self.store.db.execute("SELECT * FROM events WHERE id=?", (identity,)).fetchone()
        return merge_event(self.store, dict(row))

    def test_daemon_down_capture_is_durable_and_replay_is_idempotent(self):
        event = self.event("PostToolUse", tool_use_id="call-a", tool_name="Bash", tool_response="ok")
        hook(event, self.settings)
        hook(event, self.settings)
        self.assertEqual(len(self.store.pending()), 1)
        second = {**event, "tool_use_id": "call-b"}
        hook(second, self.settings)
        self.assertEqual(len(self.store.pending()), 2)

    def test_scoping_and_context_delivery_are_independent_of_notices(self):
        self.store.notice({"id": "review-1", "message": "已更新技能"})
        atomic_json(self.settings.state / "context.json", {"version": "v1", "summary": "验证前保留输入证据"})
        first = hook(self.event("SessionStart"), self.settings)
        self.assertIn("已更新", first["systemMessage"])
        self.assertIn("验证前", first["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(hook(self.event("UserPromptSubmit", prompt="next"), self.settings), {})
        second_chat = hook({**self.event("SessionStart"), "session_id": "chat-b"}, self.settings)
        self.assertIn("hookSpecificOutput", second_chat)
        self.assertEqual(hook({**self.event("Stop"), "cwd": "/unrelated"}, self.settings), {})
        self.assertEqual(len(self.store.consume("os-notification")), 1)

    def test_running_command_does_not_become_idle_until_late_completion(self):
        self.merge(self.event("UserPromptSubmit", prompt="Run validation"))
        self.merge(self.event("PostToolUse", tool_use_id="exec", tool_name="Bash", tool_input={"command": "long-test"},
                              tool_response={"session_id": 17, "output": "running"}))
        stopped = self.merge(self.event("Stop", last_assistant_message="Waiting for validation"))
        self.assertEqual(stopped["status"], "incomplete")
        complete = self.merge(self.event("PostToolUse", tool_use_id="poll", tool_name="write_stdin", tool_input={"session_id": 17},
                                        tool_response={"exit_code": 0, "output": "passed"}))
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(len(observation(complete)["tools"]), 1)

    def test_missing_source_identity_is_explicit_not_content_hash_counting(self):
        data = self.merge(self.event("PostToolUse", tool_name="Bash", tool_response="ok"))
        self.assertIn("missing_tool_use_id", data["coverage"])
        self.assertFalse(data["tools"])

    def test_redacts_credentials_without_changing_ordinary_evidence(self):
        result = redact({"api_key": "example", "output": "API_KEY=secret-value\nTest passed", "exit_code": 0})
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("Test passed", result["output"])

    def test_idle_flush_requires_terminal_latest_turn_not_silence(self):
        from hermes_codex_learning.daemon import Bridge
        from dataclasses import replace

        class API:
            def __init__(self):
                self.flushes = []

            def open(self, source):
                return {"handle": source["chat"]}

            def maintenance(self, **kwargs):
                self.flushes.append(kwargs)

        api = API()
        bridge = Bridge(replace(self.settings, idle_seconds=0, idle_interval=0), api)
        self.merge(self.event("UserPromptSubmit", prompt="Long task"))
        bridge.idle_reviews()
        self.assertFalse(api.flushes)
        self.merge(self.event("Interrupt"))
        self.merge(self.event("UserPromptSubmit", prompt="Retry", turn_id="turn-b"))
        self.merge(self.event("Stop", last_assistant_message="Done", turn_id="turn-b"))
        bridge.idle_reviews()
        self.assertEqual(len(api.flushes), 1)
        bridge.store.close()


if __name__ == "__main__":
    unittest.main()
