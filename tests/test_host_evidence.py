import json
import tempfile
import unittest
import os
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from hermes_codex_learning.host_evidence import read_turn


class HostEvidenceTests(unittest.TestCase):
    def test_pending_semantic_turn_recovers_from_verified_noncommand_terminal_items(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.daemon import Bridge
        from hermes_codex_learning.normalize import merge_event
        settings=Settings(self.root/"home",self.project,self.root,Path("/usr/bin/python3"))
        observed=[]
        api=SimpleNamespace(open=lambda source:{"handle":"h"},observe=lambda *a,**kw:observed.append((a,kw)))
        bridge=Bridge(settings,api)
        try:
            events=[
                {"hook_event_name":"UserPromptSubmit","prompt":"Synthetic task"},
                {"hook_event_name":"PreToolUse","tool_use_id":"mcp","tool_name":"mcp__hermes_learning__context"},
                {"hook_event_name":"PreToolUse","tool_use_id":"edit","tool_name":"apply_patch"},
                {"hook_event_name":"Stop","last_assistant_message":"Synthetic task ended"},
            ]
            for event in events:
                value={**event,"session_id":"chat","turn_id":"turn","cwd":str(self.project),
                       "bridge_project":str(self.project),"bridge_host":"host","transcript_path":str(self.path)}
                identity=bridge.store.capture(value)
                merge_event(bridge.store,bridge.store.db.execute("SELECT * FROM events WHERE id=?",(identity,)).fetchone())
            self.write([{"type":"event_msg","payload":{"type":"item_completed","turn_id":"turn","item":item}} for item in [
                {"type":"McpToolCall","id":"mcp","status":"failed"},
                {"type":"FileChange","id":"edit","status":"completed"},
            ]])
            with patch.dict(os.environ,{"CODEX_HOME":str(self.root)}):
                bridge.retry_completed_inputs()
                bridge.store.set("last_input_retry",0)
                bridge.retry_completed_inputs()
            self.assertEqual(len(observed),1)
            self.assertEqual(observed[0][0][1]["status"],"completed")
            self.assertEqual({tool["call_id"]:tool["status"] for tool in observed[0][0][1]["tools"]},
                             {"mcp":"failed","edit":"completed"})
        finally:
            bridge.store.close()

    def test_mcp_terminal_supplement_preserves_already_admitted_content(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.store import Store
        from hermes_codex_learning.host_evidence import supplement
        settings=Settings(self.root/"home",self.project,self.root,Path("/usr/bin/python3"))
        store=Store(settings.state)
        try:
            source={"host":"host","project":str(self.project),"chat":"chat","turn":"turn"}
            store.capture({"session_id":"chat","turn_id":"turn","cwd":str(self.project),
                           "bridge_host":"host","bridge_project":str(self.project),"transcript_path":str(self.path)})
            self.write([{"type":"event_msg","payload":{"type":"item_completed","turn_id":"turn",
                        "item":{"type":"McpToolCall","id":"mcp","status":"completed"}}}])
            content={"output":"Already admitted public evidence."}
            data={"source":source,"user":"task","tools":{"mcp":{"status":"unknown","result":content}},
                  "status":"incomplete","_ended_by":"Stop"}
            with patch.dict(os.environ,{"CODEX_HOME":str(self.root)}):
                result=supplement(store,data,settings=settings)
            self.assertEqual(result["tools"]["mcp"]["result"],content)
            self.assertEqual(result["status"],"completed")
        finally:
            store.close()

    def test_mcp_denial_and_file_change_are_terminal_without_importing_bodies(self):
        def row(item):
            return {"type":"event_msg","payload":{"type":"item_completed","turn_id":"turn","item":item}}
        self.write([
            row({"type":"McpToolCall","id":"mcp","status":"failed","error":{"message":"PRIVATE denial detail"}}),
            row({"type":"FileChange","id":"edit","status":"completed","stdout":"PRIVATE diff"}),
        ])
        result = read_turn(self.path,"chat","turn",str(self.project),sessions_root=self.sessions,include_task=False)
        self.assertEqual(result["commands"]["mcp"]["status"],"failed")
        self.assertEqual(result["commands"]["edit"]["status"],"completed")
        self.assertNotIn("PRIVATE",str(result))

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
