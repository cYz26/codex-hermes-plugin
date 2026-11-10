import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.store import Store


class ApplicationFeedbackTests(unittest.TestCase):
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
            finally:
                store.close()
