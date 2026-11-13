import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.store import Store, digest


class ApplicationFeedbackTests(unittest.TestCase):
    def test_wrong_reference_is_superseded_by_the_copyable_same_version_feedback(self):
        from hermes_codex_learning.feedback import reconcile
        from hermes_codex_learning.normalize import merge_event
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            settings = Settings(project / "home", project, project, Path("/usr/bin/python3"))
            bridge = Bridge(settings, SimpleNamespace())
            self.addCleanup(bridge.store.close)
            token = "fixture"
            bridge.store.set("issued-delivery:" + token, {"name": "fixture", "version": "v1",
                             "project": str(project), "source_event": ["chat", "turn", "load"]})
            scope = {"cwd": str(project), "chat": "chat", "turn": "turn"}
            common = {"cwd": str(project), "bridge_project": str(project), "session_id": "chat", "turn_id": "turn"}

            def event(kind, **fields):
                identifier = bridge.store.capture({**common, "hook_event_name": kind, **fields})
                row = bridge.store.db.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone()
                merge_event(bridge.store, row)

            def submit(source_event):
                return bridge.rpc("record_application", {"cwd": str(project), "delivery_token": token,
                                  "stage": "verified", "source_events": [source_event]})

            wrong = {"chat": "chat", "turn": "turn", "tool_call": "call_wrong"}
            event("UserPromptSubmit", prompt="Try the released method")
            self.assertEqual(submit(wrong)["validation_reason"], "awaiting_host_completion")
            event("PostToolUse", tool_use_id="exec-real", tool_name="Bash",
                  tool_input={"command": "npm test"}, tool_response={"exit_code": 0})
            event("Stop", last_assistant_message="Verification finished")
            old = submit(wrong)
            self.assertEqual(old["validation_reason"], "reference_not_found")
            lookup = bridge.rpc("feedback_status", scope)
            self.assertEqual(lookup["deliveries"], [{"name": "fixture", "version": "v1"}])
            ref = lookup["test_references"][0]
            self.assertEqual(ref["source_event"]["tool_call"], "exec-real")
            current = submit(ref["source_event"])
            self.assertEqual(current["validation"], "host_test_passed")
            self.assertEqual(current["test_outcomes"], [ref])
            reconcile(bridge.store)
            result = bridge.rpc("feedback_status", {**scope, "receipt_id": old["id"]})["receipt"]
            self.assertEqual((result["validation"], result["superseded_by"]), ("superseded", current["id"]))
            self.assertEqual(bridge.rpc("feedback_status", {**scope, "chat": "unrelated",
                             "receipt_id": current["id"]})["state"], "receipt_unavailable")
            for turn_id in ("new-turn", "turn"):
                bridge.store.capture({**common, "turn_id": turn_id, "hook_event_name": "PostToolUse",
                                      "tool_use_id": "late-result", "tool_name": "Bash"})
            latest = bridge.rpc("feedback_status", {"cwd": str(project), "chat": "chat"})
            self.assertEqual(latest["turn"], "new-turn")

    def test_hook_delivery_and_test_results_work_without_priority_scheduling(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            settings = Settings(project / "home", project, project, Path("/usr/bin/python3"), capture_scope="global")
            bridge = Bridge(settings, SimpleNamespace(version=1))
            self.addCleanup(bridge.store.close)
            from hermes_codex_learning.read_receipts import issue
            skill = {"name": "learned-example", "version": "v1", "file_path": "SKILL.md", "content_hash": "hash"}
            token = issue(settings.state, skill, str(project))
            source = {"cwd": str(project), "session_id": "chat", "turn_id": "turn"}
            hook({**source, "hook_event_name": "UserPromptSubmit", "prompt": "Apply the fixture skill"}, settings)
            with self.assertRaises(PermissionError):
                bridge.rpc("record_application", {"cwd": str(project), "delivery_token": token, "stage": "applied"})
            hook({**source, "hook_event_name": "PostToolUse", "tool_use_id": "load",
                  "tool_name": "mcp__hermes_learning__skill_load",
                  "tool_response": {"structuredContent": {**skill, "delivery_token": token}}}, settings)
            applied = bridge.rpc("record_application", {"cwd": str(project), "delivery_token": token, "stage": "applied"})
            self.assertEqual(applied["delivery_source_event"], ["chat", "turn", "load"])

            cases = (
                ("unit", "python3 -m unittest", {"exit_code": 0}, "host_test_passed"),
                ("package", "npm test", {"exit_code": 1}, "host_test_failed"),
                ("release", "python3 -B scripts/check_release.py", {"exit_code": 0}, "host_test_passed"),
                ("env", "PYTHONPATH=src python3 -B -m unittest", {"exit_code": 0}, "host_test_passed"),
                ("unknown", "npm test", {"exit_code": True}, "pending"),
                ("transport", "npm test", {"isError": True}, "pending"),
                ("interrupted", "npm test", {"interrupted": True}, "pending"),
                ("cancelled", "npm test", {"interrupted": True, "isError": True, "exit_code": 130}, "pending"),
                ("shell", "npm test || true", {"exit_code": 0}, "pending"),
                ("list", "pytest --collect-only", {"exit_code": 0}, "pending"),
                ("optional", "npm test --if-present", {"exit_code": 0}, "pending"),
            )
            from hermes_codex_learning.normalize import merge_event
            last_row = 0
            for call, command, response, expected in cases:
                with self.subTest(command=command, response=response):
                    hook({**source, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_use_id": call, "tool_input": {"command": command}}, settings)
                    hook({**source, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                          "tool_use_id": call, "tool_response": response}, settings)
                    for row in bridge.store.db.execute("SELECT rowid,* FROM events WHERE rowid>? ORDER BY rowid",
                                                       (last_row,)).fetchall():
                        merge_event(bridge.store, row)
                        last_row = row["rowid"]
                    receipt = bridge.rpc("record_application", {
                        "cwd": str(project), "delivery_token": token, "stage": "verified",
                        "source_events": [{"chat": "chat", "turn": "turn", "tool_call": call}],
                    })
                    self.assertEqual(receipt["validation"], expected)
                    self.assertEqual(receipt["effectiveness"], "not_inferred")
            self.assertEqual(bridge.store.db.execute("SELECT COUNT(*) FROM review_priority").fetchone()[0], 0)

    def test_application_claim_requires_matching_project_delivery_and_test_evidence(self):
        from hermes_codex_learning.feedback import record, reconcile
        from hermes_codex_learning.normalize import merge_event
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                store.set("issued-delivery:receipt", {"name":"learned-example","version":"v1","project":"/p"})
                with self.assertRaises(PermissionError):
                    record(store, "receipt", "/other", "verified", [])
                params = [{"chat":"c","turn":"t","tool_call":"test"}]
                receipt = record(store,"receipt","/p","verified",params)
                self.assertEqual(receipt["application"], "self_reported")
                self.assertEqual(receipt["validation"], "pending")
                event = {"hook_event_name":"PostToolUse","cwd":"/p","bridge_project":"/p",
                         "session_id":"c","turn_id":"t","tool_use_id":"test","tool_name":"Bash",
                         "bridge_action":"test","tool_response":{"exit_code":0}}
                event_id = store.capture(event)
                merge_event(store, store.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
                reconcile(store)
                confirmed = record(store,"receipt","/p","verified",params)
                self.assertEqual(confirmed["validation"], "host_test_passed")
                self.assertEqual(confirmed["effectiveness"], "not_inferred")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM application_evidence").fetchone()[0], 1)
                legacy = {"source": {"project": "/p"}, "tools": {"test": {
                    "name": "Bash", "arguments": {"cmd": "npm test"}, "action": "execute",
                    "status": "completed", "result": {"exit_code": 0}}}}
                store.db.execute("INSERT INTO turns(id,payload,revision,activity) VALUES (?,?,?,?)",
                                 (digest(["c", "legacy"]), json.dumps(legacy), "fixture", 1))
                store.db.commit()
                self.assertEqual(record(store, "receipt", "/p", "verified", [{**params[0], "turn": "legacy"}])["validation"],
                                 "host_test_passed")
            finally:
                store.close()
