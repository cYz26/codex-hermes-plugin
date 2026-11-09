import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.normalize import merge_event, observation
from hermes_codex_learning.store import Store


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.event("UserPromptSubmit", prompt="Synthetic command fixture")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, name, **fields):
        payload = {"hook_event_name": name, "session_id": "chat", "turn_id": "turn",
                   "cwd": self.temp.name, **fields}
        eid = self.store.capture(payload)
        row = self.store.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return merge_event(self.store, dict(row))

    def test_nested_running_result_stays_incomplete_until_real_exit(self):
        self.event("PostToolUse", tool_name="exec_command", tool_use_id="start",
                   tool_response={"content": [{"type": "text", "text": json.dumps({"session_id": 17, "output": "running"})}]})
        pending = self.event("Stop", last_assistant_message="Waiting")
        self.assertEqual(pending["status"], "incomplete")
        done = self.event("PostToolUse", tool_name="write_stdin", tool_use_id="poll", tool_input={"session_id": 17},
                          tool_response={"exit_code": 0, "output": "passed"})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(done["tools"]), 1)
        self.assertEqual(done["tools"]["start"]["status"], "completed")

    def test_pre_tool_without_result_cannot_become_completed(self):
        self.event("PreToolUse", tool_name="exec_command", tool_use_id="start", tool_input={"cmd": "run"})
        result = self.event("Stop", last_assistant_message="")
        self.assertEqual(result["status"], "incomplete")
        done = self.event("PostToolUse", tool_name="exec_command", tool_use_id="start", tool_response={"exit_code": 0})
        self.assertEqual(done["status"], "completed")
        self.assertNotIn("_ended_by", observation(done))

    def test_unrecognized_command_envelope_is_not_treated_as_success(self):
        self.event("PostToolUse", tool_name="functions.exec", tool_use_id="start",
                   tool_response={"content": [{"type": "text", "text": "arbitrary nested output"}]})
        result = self.event("Stop", last_assistant_message="Done")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unrecognized_execution_result:start", result["coverage"])

    def test_tool_failure_is_terminal_but_preserved_as_failure(self):
        self.event("PostToolUse", tool_name="exec_command", tool_use_id="start",
                   tool_response={"exit_code": 2, "output": "test failed"})
        result = self.event("Stop", last_assistant_message="Test failed")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools"]["start"]["status"], "failed")
        self.assertEqual(result["tools"]["start"]["result"]["exit_code"], 2)

    def test_interrupt_is_not_rewritten_by_late_exit(self):
        self.event("PostToolUse", tool_name="exec_command", tool_use_id="start", tool_response={"session_id": 17})
        self.event("Interrupt")
        late = self.event("PostToolUse", tool_name="write_stdin", tool_use_id="poll", tool_input={"session_id": 17},
                          tool_response={"exit_code": 0})
        self.assertEqual(late["status"], "interrupted")

    def test_stop_after_interrupt_preserves_interruption(self):
        self.event("Interrupt")
        stopped = self.event("Stop", last_assistant_message="Cancelled")
        self.assertEqual(stopped["status"], "interrupted")

    def test_unknown_flat_command_result_retains_a_coverage_gap(self):
        self.event("PostToolUse", tool_name="exec_command", tool_use_id="unknown", tool_response={"unrecognized": True})
        stopped = self.event("Stop", last_assistant_message="Done")
        self.assertEqual(stopped["status"], "incomplete")
        self.assertIn("unrecognized_execution_result:unknown", stopped["coverage"])

    def test_free_text_and_boolean_exit_codes_do_not_prove_command_completion(self):
        for index, response in enumerate(("All checks passed", None, {"exit_code": False}, {"exit_code": True})):
            with self.subTest(response=response):
                identifier = f"unknown-{index}"
                result = self.event("PostToolUse", tool_name="exec_command", tool_use_id=identifier,
                                    tool_response=response)
                self.assertEqual(result["tools"][identifier]["status"], "unknown")
                self.assertIn(f"unrecognized_execution_result:{identifier}", result["coverage"])

    def test_generic_tool_session_field_is_not_a_command_handle(self):
        self.event("PostToolUse", tool_name="calendar_lookup", tool_use_id="lookup",
                   tool_response={"session_id": "business-session", "success": True})
        result = self.event("Stop", last_assistant_message="Done")
        self.assertEqual(result["status"], "completed")

    def test_duplicate_pre_event_does_not_reopen_finished_call(self):
        self.event("PreToolUse", tool_name="exec_command", tool_use_id="start")
        self.event("PostToolUse", tool_name="exec_command", tool_use_id="start", tool_response={"exit_code": 0})
        self.event("PreToolUse", tool_name="exec_command", tool_use_id="start")
        result = self.event("Stop", last_assistant_message="Done")
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
