import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import Store, digest


class PendingReviewCommandTests(unittest.TestCase):
    def test_project_can_inspect_an_empty_queue_without_starting_a_model(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "project"
            project.mkdir()
            alias = base / "project-link"
            alias.symlink_to(project, target_is_directory=True)
            settings = base / "bridge.json"
            settings.write_text(json.dumps({
                "home": str(base / "learning-home"), "project": str(alias),
                "source": str(base / "unused-source"), "python": sys.executable,
                "capture_scope": "global",
            }))
            result = subprocess.run(
                [sys.executable, "-B", str(root / "scripts/bridge"), "approvals", "list",
                 "--settings", str(settings), "--cwd", str(alias)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            value = json.loads(result.stdout)
            self.assertEqual(value["project"], str(project))
            self.assertEqual(value["proposals"], [])
            self.assertEqual(value["counts"]["pending"], 0)
            self.assertFalse((base / "learning-home" / "memories").exists())


class PendingReviewTests(unittest.TestCase):
    def test_diff_separates_lines_when_memory_has_no_final_newline(self):
        from hermes_codex_learning.approvals import inspect_record
        record = {"payload": {"action": "patch", "target": "memory", "old_text": "old",
                              "content": "new", "matched_entry": "old"}}
        result = inspect_record(record, ["old"])
        self.assertIn("\n-old\n+new\n", result["changes"][0]["diff"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project-a"
        self.foreign = self.base / "project-b"
        self.project.mkdir()
        self.foreign.mkdir()
        self.settings = Settings(self.base / "learning", self.project, self.base / "native",
                                 Path(sys.executable), capture_scope="global")
        self.store = Store(self.settings.state)
        self.native = Mock()
        from hermes_codex_learning.approvals import PendingReviews
        self.reviews = PendingReviews(self.settings, self.store, self.native)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def record(self, identifier, project=None, content="new rule", state="pending", created=1):
        project = project or self.project
        self.store.notice({"id": "review:" + identifier, "kind": "review.finished", "project": str(project),
                           "staged": [{"tool": "memory", "result": {"pending_id": identifier}}]})
        return {"record": {"id": identifier, "created_at": created, "origin": "background_review",
                           "summary": "revise fixture rule", "payload": {
                               "action": "batch", "target": "memory", "operations": [
                                   {"action": "patch", "old_text": "old rule", "content": content,
                                    "matched_entry": "old rule"}]}},
                "state": state, "reason": "fixture state", "changes": [
                    {"action": "patch", "before": "old rule", "after": content, "diff": "fixture diff"}]}

    def test_query_is_scoped_and_identifies_duplicate_proposals(self):
        first = self.record("11111111")
        second = self.record("22222222", created=2)
        self.record("33333333", self.foreign, content="foreign private rule")
        self.native.return_value = [second, first]
        result = self.reviews.list(str(self.project))
        self.native.assert_called_once_with("inspect", ids=["11111111", "22222222"])
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual(result["counts"]["duplicate"], 1)
        self.assertEqual(result["proposals"][1]["duplicate_of"], "11111111")
        self.assertFalse(result["proposals"][1]["can_approve"])
        self.assertNotIn("foreign", json.dumps(result))
        self.assertFalse(result["automatic_approval"])

    def test_foreign_identifier_is_rejected_before_native_access(self):
        self.record("33333333", self.foreign)
        with self.assertRaises(PermissionError):
            self.reviews.list(str(self.project), "33333333")
        self.native.assert_not_called()

    def test_approval_of_a_changed_proposal_is_rejected_before_writing(self):
        item = self.record("11111111")
        self.native.return_value = [item]
        revision = self.reviews.list(str(self.project))["proposals"][0]["revision"]
        item["record"]["payload"]["operations"][0]["content"] = "different unreviewed rule"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.reviews.decide(str(self.project), "11111111", revision, "approve")
        self.assertTrue(all(call.args[0] == "inspect" for call in self.native.call_args_list))

    def test_cleanup_is_previewed_and_keeps_the_canonical_pending_proposal(self):
        items = [self.record("11111111"), self.record("22222222", created=2),
                 self.record("33333333", content="obsolete rule", state="stale", created=3)]
        def native(action, **params):
            if action == "inspect":
                return list(items)
            self.assertEqual(action, "reject")
            items[:] = [item for item in items if item["record"]["id"] != params["id"]]
            return {"state": "rejected", "applied": False, "pending_removed": True}
        self.native.side_effect = native
        preview = self.reviews.prune(str(self.project))
        self.assertEqual([p["id"] for p in preview["candidates"]], ["22222222", "33333333"])
        self.assertFalse(preview["applied"])
        self.assertTrue(all(call.args[0] == "inspect" for call in self.native.call_args_list))
        result = self.reviews.prune(str(self.project), preview["review_revision"], apply=True)
        self.assertEqual(result["removed"], ["22222222", "33333333"])
        self.assertEqual([item["record"]["id"] for item in items], ["11111111"])

    def test_cleanup_stops_when_the_reviewed_queue_changes(self):
        items = [self.record("11111111"), self.record("22222222", created=2)]
        self.native.side_effect = lambda action, **params: list(items)
        preview = self.reviews.prune(str(self.project))
        items.append(self.record("33333333", content="unreviewed proposal", created=3))
        with self.assertRaisesRegex(RuntimeError, "queue changed"):
            self.reviews.prune(str(self.project), preview["review_revision"], apply=True)
        self.assertTrue(all(call.args[0] == "inspect" for call in self.native.call_args_list))

    def test_a_registered_identifier_cannot_expose_another_projects_target(self):
        item = self.record("11111111")
        secret = "foreign private memory target"
        item["record"]["payload"]["operations"][0]["matched_entry"] = secret
        item["changes"][0]["before"] = secret
        self.store.set("knowledge-index-v1", {"memory": {
            digest(["MEMORY.md", secret]): {"project": str(self.foreign)}}})
        self.native.return_value = [item]
        view = self.reviews.list(str(self.project))
        self.assertFalse(view["proposals"][0]["can_approve"])
        self.assertNotIn(secret, json.dumps(view))

    def test_inactive_enterprise_binding_blocks_body_access(self):
        self.record("11111111")
        self.native.return_value = []
        from hermes_codex_learning.approvals import PendingReviews
        settings = replace(self.settings, data_policy={"mode": "trusted_enterprise"})
        with self.assertRaises(PermissionError):
            PendingReviews(settings, self.store, self.native).list(str(self.project))
        self.native.assert_not_called()
