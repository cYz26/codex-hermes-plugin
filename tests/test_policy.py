import hashlib
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from hermes_codex_learning.policy import admit, published_resource, sharing_check
from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import Store
from hermes_codex_learning.knowledge import context_view


class PolicyTests(unittest.TestCase):
    def test_model_admission_is_not_permission_to_publish_globally(self):
        policy = {"assessment_command": ["/assessment"]}
        verdict = {"allowed": True, "classification": "approved", "assessment_id": "fixture-assessment"}
        import json
        with patch("hermes_codex_learning.policy.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(verdict))):
            self.assertTrue(admit({"body": "fixture"}, policy, "/project")["allowed"])
            self.assertFalse(admit({"body": "fixture"}, policy, "/project", purpose="share")["allowed"])

    def test_mixed_source_skill_is_not_private_to_either_project(self):
        context = {"checked_publication": True, "memories": {},
                   "skills": [{"name": "mixed", "description": "private details"}],
                   "bridge_provenance": {"skills": {"mixed": {"projects": ["/a", "/b"]}}}}
        self.assertEqual(context_view(context, "/a")["skills"], [])
        self.assertEqual(context_view(context, "/b")["skills"], [])

    def test_rejected_hook_retains_only_a_gap_not_the_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "home", root, root, Path("/usr/bin/python3"), data_policy={"enabled": True})
            hook({"hook_event_name": "UserPromptSubmit", "cwd": directory,
                  "session_id": "chat", "turn_id": "turn", "prompt": "PRIVATE-BODY"}, settings)
            store = Store(settings.state)
            try:
                self.assertEqual(store.pending(), [])
                self.assertEqual(store.get("coverage_gaps")[0]["body_retained"], False)
                self.assertNotIn("PRIVATE-BODY", str(store.db.execute("SELECT * FROM meta").fetchall()))
            finally:
                store.close()

    def test_unknown_source_is_withheld_without_retaining_body(self):
        result = admit({"prompt": "private body"}, {"enabled": True}, "/project")
        self.assertFalse(result["allowed"])
        self.assertNotIn("private body", str(result))

    def test_synthetic_scope_does_not_admit_credentials(self):
        result = admit({"api_key": "dummy-sensitive"}, {"synthetic_roots": ["/fixture"]}, "/fixture/a")
        self.assertEqual(result["reason"], "credential_material")

    def test_generic_candidate_can_publish_but_project_details_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SKILL.md"
            body = "---\nname: portable\nlearning_scope: global_candidate\n---\nValidate input before writing output.\n"
            path.write_text(body)
            value = {"name": "portable", "content": body, "resource_dir": directory, "version": "v1",
                     "files": {"SKILL.md": hashlib.sha256(path.read_bytes()).hexdigest()}}
            self.assertTrue(sharing_check(value, ["/project/a"], ["private-label-a"])["allowed"])
            path.write_text(body + "private-label-a")
            value.update(content=path.read_text(), files={"SKILL.md": hashlib.sha256(path.read_bytes()).hexdigest()})
            self.assertEqual(sharing_check(value, ["/project/a"], ["private-label-a"])["reason"], "project_private_material")

    def test_published_resource_is_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SKILL.md"
            path.write_text("approved")
            record = {"artifact": {"resource_dir": directory,
                      "files": {"SKILL.md": hashlib.sha256(path.read_bytes()).hexdigest()}}}
            path.write_text("replaced")
            with self.assertRaisesRegex(ValueError, "changed"):
                published_resource(record)
