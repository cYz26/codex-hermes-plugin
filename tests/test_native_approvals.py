"""Exercise actual native pending replay in disposable learning homes, without a provider."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.approvals import PendingReviews
from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import Store, digest


@unittest.skipUnless(os.environ.get("HERMES_LEARNING_NATIVE_SOURCE"), "Requires the verified native source and interpreter")
class NativeApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.home = self.base / "learning-home"
        (self.home / "memories").mkdir(parents=True)
        self.memory = self.home / "memories/MEMORY.md"
        self.before, self.after = "Project fixture: old rule", "Project fixture: reviewed new rule"
        self.foreign = "Foreign project fact must survive"
        self.memory.write_text(self.before + "\n§\n" + self.foreign + "\n")
        self.settings = Settings(self.home, self.project, Path(os.environ["HERMES_LEARNING_NATIVE_SOURCE"]),
                                 Path(sys.executable), capture_scope="global")
        self.store = Store(self.settings.state)
        self.store.set("knowledge-index-v1", {"memory": {
            digest(["MEMORY.md", self.before]): {"project": str(self.project)},
            digest(["MEMORY.md", self.foreign]): {"project": str(self.base / "foreign")}}, "skills": {}})
        self.reviews = PendingReviews(self.settings, self.store)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def stage(self, content=None):
        program = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from tools.write_approval import stage_write,MEMORY;"
            "r=stage_write(MEMORY,json.load(sys.stdin),summary='fixture revision',origin='background_review');"
            "print(json.dumps({'id':r['id']}))"
        )
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG"}}
        environment.update(self.settings.native_environment())
        payload = {"action": "batch", "target": "memory", "operations": [
            {"action": "patch", "old_text": self.before, "content": content or self.after,
             "matched_entry": self.before}]}
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(self.settings.source)],
                                input=json.dumps(payload), env=environment, capture_output=True,
                                text=True, timeout=20, check=True)
        identifier = json.loads(result.stdout)["id"]
        self.store.notice({"id": "fixture:" + identifier, "kind": "review.finished", "project": str(self.project),
                           "staged": [{"tool": "memory", "result": {"pending_id": identifier}}]})
        return identifier

    def test_reviewed_approval_uses_native_write_and_preserves_foreign_memory(self):
        identifier = self.stage()
        proposal = self.reviews.list(str(self.project), identifier)["proposals"][0]
        self.assertEqual(proposal["operations"][0]["before"], self.before)
        self.assertEqual(proposal["operations"][0]["after"], self.after)
        self.assertIn(self.before, self.memory.read_text())
        result = self.reviews.decide(str(self.project), identifier, proposal["revision"], "approve")
        self.assertEqual(result["state"], "approved", result)
        self.assertTrue(result["applied"])
        self.assertTrue(result["pending_removed"])
        text = self.memory.read_text()
        self.assertIn(self.after, text)
        self.assertIn(self.foreign, text)
        self.assertNotIn(self.before, text)
        self.assertFalse((self.home / "pending/memory" / (identifier + ".json")).exists())
        origin = self.store.get("approved-memory-origins")[digest(["MEMORY.md", self.after])]
        self.assertEqual(origin["project"], str(self.project))
        with self.assertRaises(RuntimeError):
            self.reviews.decide(str(self.project), identifier, proposal["revision"], "approve")
        self.assertEqual(self.memory.read_text(), text)

    def test_rejection_removes_only_the_proposal(self):
        identifier = self.stage()
        proposal = self.reviews.list(str(self.project), identifier)["proposals"][0]
        before = self.memory.read_bytes()
        result = self.reviews.decide(str(self.project), identifier, proposal["revision"], "reject")
        self.assertEqual(result["state"], "rejected")
        self.assertFalse(result["applied"])
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertFalse((self.home / "pending/memory" / (identifier + ".json")).exists())

    def test_changed_original_cannot_be_overwritten_and_proposal_is_retained(self):
        identifier = self.stage()
        proposal = self.reviews.list(str(self.project), identifier)["proposals"][0]
        changed = "Project fixture: independently changed rule\n§\n" + self.foreign + "\n"
        self.memory.write_text(changed)
        with self.assertRaisesRegex(RuntimeError, "cannot be applied"):
            self.reviews.decide(str(self.project), identifier, proposal["revision"], "approve")
        self.assertEqual(self.memory.read_text(), changed)
        self.assertTrue((self.home / "pending/memory" / (identifier + ".json")).exists())

    def test_native_payload_changed_after_review_cannot_be_applied(self):
        identifier = self.stage()
        proposal = self.reviews.list(str(self.project), identifier)["proposals"][0]
        pending = self.home / "pending/memory" / (identifier + ".json")
        value = json.loads(pending.read_text())
        value["payload"]["operations"][0]["content"] = "Unreviewed changed proposal"
        pending.write_text(json.dumps(value))
        before = self.memory.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.reviews.decide(str(self.project), identifier, proposal["revision"], "approve")
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertTrue(pending.exists())

    def test_duplicate_cleanup_uses_native_rejection_without_changing_memory(self):
        first = self.stage()
        second = self.stage()
        before = self.memory.read_bytes()
        preview = self.reviews.prune(str(self.project))
        self.assertEqual([p["id"] for p in preview["candidates"]], [second])
        self.assertTrue((self.home / "pending/memory" / (second + ".json")).exists())
        result = self.reviews.prune(str(self.project), preview["review_revision"], apply=True)
        self.assertEqual(result["removed"], [second])
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertTrue((self.home / "pending/memory" / (first + ".json")).exists())

    @unittest.skipIf(os.environ.get("HERMES_APPROVAL_SELFTEST") == "1", "Self-test wrapper is checked by the outer suite")
    def test_builtin_selftest_reports_actual_tests_without_touching_the_live_queue(self):
        from hermes_codex_learning.approvals import self_test
        identifier = self.stage()
        before = self.memory.read_bytes()
        result = self_test(self.settings)
        self.assertEqual(result["state"], "passed", result)
        self.assertGreaterEqual(result["tests"], 5)
        self.assertFalse(result["live_knowledge_changed"])
        self.assertEqual(self.memory.read_bytes(), before)
        self.assertTrue((self.home / "pending/memory" / (identifier + ".json")).exists())
