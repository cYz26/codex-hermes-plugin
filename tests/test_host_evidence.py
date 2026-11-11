import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.host_evidence import read_turn


class HostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.path = self.sessions / "thread.jsonl"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        header = {"type": "session_meta", "payload": {"id": "chat", "cwd": str(self.project), "cli_version": "0.167.0"}}
        self.path.write_text("\n".join(json.dumps(row) for row in [header, *rows]) + "\n")

    def test_reads_only_bound_turn_task_and_actual_command_exit(self):
        item = {"type": "FunctionCallOutput", "id": "input-item", "namespace": "codex_app", "name": "send_message_to_thread",
                "output": {"text": "<codex_delegation><source_thread_id>parent</source_thread_id><input>synthetic task</input></codex_delegation>", "truncated": False}}
        def row(turn, item):
            return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": item}}
        self.write([row("old", {**item, "id": "older"}), row("turn", item),
                    row("turn", {"type": "AgentMessage", "content": "NOTICE-MUST-NOT-ENTER"}),
                    row("turn", {"type": "Reasoning", "raw_content": "REASONING-MUST-NOT-ENTER"}),
                    row("turn", {"type": "CommandExecution", "id": "tool", "status": "failed", "exit_code": 2})])
        result = read_turn(self.path, "chat", "turn", str(self.project), sessions_root=self.sessions)
        self.assertEqual(result["input"]["prompt"], "synthetic task")
        self.assertEqual(result["commands"]["tool"]["exit_code"], 2)
        self.assertNotIn("MUST-NOT-ENTER", str(result))
        metadata = read_turn(self.path, 'chat', 'turn', str(self.project), sessions_root=self.sessions,
                             include_task=False)
        self.assertIsNone(metadata['input'])
        self.assertEqual(metadata['commands']['tool']['exit_code'], 2)
        self.assertNotIn('synthetic task', str(metadata))

    def test_rejects_foreign_session_and_workspace(self):
        self.write([])
        for chat, project in [("other", self.project), ("chat", self.root / "other")]:
            with self.assertRaises(ValueError):
                read_turn(self.path, chat, "turn", str(project), sessions_root=self.sessions)

    def test_symlink_cannot_redirect_the_transcript(self):
        self.write([])
        link = self.sessions / "link.jsonl"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            read_turn(link, "chat", "turn", str(self.project), sessions_root=self.sessions)

    def test_partial_trailing_record_does_not_import_partial_content(self):
        self.write([])
        with self.path.open("a") as stream:
            stream.write('{"type":"response_item"')
        self.assertIsNone(read_turn(self.path, "chat", "turn", str(self.project), sessions_root=self.sessions)["input"])
