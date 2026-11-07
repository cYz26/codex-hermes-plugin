import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.config import Settings
from hermes_codex_learning.capture import hook
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.delegation import SUGGESTION_PREFIX, register_delegation, task_input
from hermes_codex_learning.store import Store


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "home", self.root / "project", self.root, Path("/usr/bin/python3"))
        self.store = Store(self.settings.state)
        self.parent = {"hook_event_name": "PostToolUse", "session_id": "parent", "turn_id": "parent-turn",
                       "tool_use_id": "create-call", "tool_name": "mcp__codex_app__create_thread",
                       "bridge_host": "host", "bridge_project": str(self.settings.project),
                       "cwd": str(self.settings.project),
                       "tool_input": {"prompt": "Implement a receipt reporter", "target": {"type": "project"}},
                       "tool_response": {"isError": False, "content": [{"type": "text", "text": json.dumps({
                           "threadId": "child", "hostId": "local"})}]}}
        self.data = {"source": {"host": "host", "project": str(self.settings.project),
                                "chat": "child", "turn": "child-turn", "executor": "Codex App"},
                     "user": "", "assistant": "Done", "tools": {}, "status": "completed", "coverage": []}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_joins_parent_hook_receipt_to_child_without_faking_user_prompt_hook(self):
        self.assertTrue(register_delegation(self.store, "parent-event", self.parent))
        enriched = task_input(self.store, self.data)
        self.assertIn("Implement a receipt reporter", enriched["user"])
        self.assertIn("not a direct user declaration", enriched["user"])
        self.assertIn("delegated_task_input:parent-event", enriched["coverage"])
        self.assertEqual(enriched["task_input_provenance"]["parent_tool_call"], "create-call")
        self.assertEqual(self.data["user"], "")
        self.assertFalse(list(self.store.db.execute("select * from events")))

    def test_initial_delegation_is_not_reused_as_the_prompt_of_another_turn(self):
        register_delegation(self.store, "parent-event", self.parent)
        self.assertTrue(task_input(self.store, self.data)["user"])
        later = {**self.data, "source": {**self.data["source"], "turn": "another-turn"}}
        self.assertEqual(task_input(self.store, later)["user"], "")

    def test_wrong_scope_failed_call_or_missing_identity_cannot_supply_task_input(self):
        for change in ({"tool_response": {"isError": True}}, {"turn_id": None},
                       {"tool_name": "mcp__other__create_thread"}, {"tool_use_id": None}):
            self.assertFalse(register_delegation(self.store, "ignored", {**self.parent, **change}))
        register_delegation(self.store, "parent-event", self.parent)
        foreign = {**self.data, "source": {**self.data["source"], "project": "/another-project"}}
        self.assertEqual(task_input(self.store, foreign)["user"], "")
        direct = {**self.data, "user": "Actual direct user prompt"}
        self.assertEqual(task_input(self.store, direct)["user"], "Actual direct user prompt")

    def test_completed_turn_recovers_after_late_parent_receipt_and_only_observes_once(self):
        class API:
            def __init__(self):
                self.observed = []

            def open(self, source):
                return {"handle": source["chat"]}

            def observe(self, handle, body, **kwargs):
                self.observed.append((handle, body, kwargs))
                return {"status": "accepted"}

        api = API()
        stop = {"hook_event_name": "Stop", "session_id": "child", "turn_id": "child-turn",
                "cwd": str(self.settings.project), "bridge_project": str(self.settings.project),
                "bridge_host": "host", "last_assistant_message": "Done"}
        stop_id = self.store.capture(stop)
        self.store.capture(self.parent)
        bridge = Bridge(self.settings, api)
        try:
            bridge.ingest()
            bridge.recover_context()
            bridge.recover_context()
            self.assertEqual(len(api.observed), 1)
            self.assertEqual(api.observed[0][0], "child")
            self.assertEqual(api.observed[0][2]["event_id"], stop_id)
            self.assertIn("Implement a receipt reporter", api.observed[0][1]["user"])
            names = [json.loads(row[0])["hook_event_name"] for row in self.store.db.execute("select payload from events")]
            self.assertEqual(names, ["Stop", "PostToolUse"])
        finally:
            bridge.store.close()

    def test_app_generated_suggestion_is_excluded_without_dropping_real_user_text(self):
        generated = {"hook_event_name": "UserPromptSubmit", "session_id": "suggestion-helper",
                     "turn_id": "suggestion-turn", "cwd": str(self.settings.project),
                     "transcript_path": None, "prompt": SUGGESTION_PREFIX + " Suggest one short message."}
        self.assertEqual(hook(generated, self.settings), {})
        self.assertEqual(hook({**generated, "hook_event_name": "Interrupt"}, self.settings), {})
        self.assertFalse(list(self.store.db.execute("select * from events")))
        self.assertEqual(self.store.get("excluded-chat:suggestion-helper")["reason"],
                         "app_generated_next_message_suggestion")
        direct = {**generated, "session_id": "real-user", "transcript_path": "/actual/session.jsonl"}
        hook(direct, self.settings)
        self.assertEqual(self.store.db.execute("select count(*) from events").fetchone()[0], 1)
        self.assertIsNone(self.store.get("excluded-chat:real-user"))

    def readback(self, text="Read-only verification", source="parent", truncated=False):
        send = {**self.parent, "tool_name": "mcp__codex_app__send_message_to_thread",
                "tool_use_id": "send-call", "tool_input": {"threadId": "child", "prompt": text}}
        send_id = self.store.capture(send)
        output = {"schemaVersion": 1, "thread": {"id": "child", "hostId": "local", "cwd": str(self.settings.project)},
                  "turns": [{"id": "follow-up", "items": [{"type": "functionCallOutput",
                     "name": "send_message_to_thread", "namespace": "codex_app", "output": {"truncated": truncated,
                     "text": f"<codex_delegation><source_thread_id>{source}</source_thread_id><input>{text}</input></codex_delegation>"}}]}]}
        read = {**self.parent, "tool_name": "mcp__codex_app__read_thread", "tool_use_id": "read-call",
                "tool_input": {"threadId": "child"}, "tool_response": {"isError": False, "structuredContent": output}}
        return send_id, read

    def test_follow_up_readback_joins_exact_sender_receipt_and_child_turn(self):
        send_id, read = self.readback()
        self.assertTrue(register_delegation(self.store, "read-event", read))
        later = {**self.data, "source": {**self.data["source"], "turn": "follow-up"}}
        result = task_input(self.store, later)
        self.assertIn("Read-only verification", result["user"])
        self.assertEqual(result["task_input_provenance"]["source_event"], send_id)
        self.assertEqual(result["task_input_provenance"]["read_event"], "read-event")
        self.assertEqual(result["task_input_provenance"]["parent_tool_call"], "send-call")
        self.assertEqual(task_input(self.store, self.data)["user"], "")

    def test_truncated_or_unmatched_readback_is_not_a_task_input(self):
        _, read = self.readback(truncated=True)
        self.assertFalse(register_delegation(self.store, "truncated", read))
        _, wrong_parent = self.readback(source="someone-else")
        self.assertFalse(register_delegation(self.store, "wrong-parent", wrong_parent))


if __name__ == "__main__":
    unittest.main()
