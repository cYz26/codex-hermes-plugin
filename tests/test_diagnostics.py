import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.diagnostics import inspect
from hermes_codex_learning.knowledge import ENTRY_DELIMITER, memory_entries
from hermes_codex_learning.learning_health import record_capacity_refusal


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "learning"
        self.home.mkdir()
        self.path = self.home / "bridge.json"
        self.path.write_text(json.dumps({"home": str(self.home), "project": str(self.root),
                                         "source": str(self.root), "python": "/usr/bin/python3"}))

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_configuration_is_reported_without_initializing(self):
        missing = self.root / "absent/bridge.json"
        result = inspect(missing)
        self.assertEqual(result["status"], "unconfigured")
        self.assertFalse(missing.parent.exists())

    def test_unreachable_service_reports_stale_snapshot_without_claiming_model_success(self):
        bridge = self.home / "bridge"
        bridge.mkdir()
        snapshot = bridge / "status.json"
        snapshot.write_text(json.dumps({"updated_at": 1, "daemon": "idle"}))
        before = snapshot.read_bytes()
        with patch("hermes_codex_learning.diagnostics.native_auth_status", return_value={"auth_mode": "native_custom_provider", "configuration_valid": True}), \
             patch("hermes_codex_learning.diagnostics.request", side_effect=ConnectionRefusedError):
            result = inspect(self.path)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["service"]["reachable"])
        self.assertTrue(result["snapshot"]["stale"])
        self.assertIsNone(result["model_probe"])
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertFalse((bridge / "bridge.sqlite").exists())

    def test_service_read_and_probe_evidence_are_separate_facts(self):
        bridge = self.home / "bridge"
        bridge.mkdir()
        (bridge / "model-probe.json").write_text(json.dumps({"run_status": "completed", "real_app": False}))
        with patch("hermes_codex_learning.diagnostics.native_auth_status", return_value={"auth_mode": "native_custom_provider", "configuration_valid": True}), \
             patch("hermes_codex_learning.diagnostics.request", return_value={"state": "idle", "error": None, "snapshot": {"updated_at": 2}, "capture_coverage_gaps": []}):
            result = inspect(self.path)
        self.assertEqual(result["status"], "reachable")
        self.assertTrue(result["service"]["reachable"])
        self.assertFalse(result["model_probe"]["real_app"])

    def test_legacy_capture_is_not_reported_as_withholding_unassessed_body(self):
        config = json.loads(self.path.read_text())
        for policy, expected in (({}, "allowed_by_legacy_configuration"), ({"enabled": True}, "withheld")):
            with self.subTest(policy=policy):
                self.path.write_text(json.dumps({**config, "data_policy": policy}))
                with patch("hermes_codex_learning.diagnostics.native_auth_status", return_value={}), \
                        patch("hermes_codex_learning.diagnostics.request", side_effect=ConnectionRefusedError):
                    result = inspect(self.path)
                self.assertEqual(result["data_admission"]["unassessed_body"], expected)

    def test_status_exposes_scoped_capacity_and_effective_policy_without_memory_text(self):
        settings = Settings(self.home, self.root / "a", self.root, Path("/usr/bin/python3"), capture_scope="global")
        context = {"memories": {"MEMORY.md": ENTRY_DELIMITER.join(["private-a", "private-b", "unassigned"])}}
        keys = [key for key, _, _ in memory_entries(context)]
        context["bridge_provenance"] = {"memory": {
            keys[0]: {"project": str(settings.project.resolve())},
            keys[1]: {"project": str((self.root / "b").resolve())},
        }, "skills": {"own": {"projects": [str(settings.project.resolve())]}}}
        context["skills"] = [{"name": "own"}, {"name": "unpublished"}]
        from hermes_codex_learning.store import atomic_json
        bridge = Bridge(settings, SimpleNamespace(version=1))
        self.addCleanup(bridge.store.close)
        bridge.native_policy = {"memory": {"MEMORY.md": {"limit_chars": 42, "enabled": True}},
                                "curator": {"enabled": True, "consolidate": False}}
        atomic_json(settings.state / "context.json", context)
        record_capacity_refusal(bridge.store, {"refused": [{"tool": "memory", "target": "memory",
            "result": {"reason": "memory_capacity_exceeded", "usage": "34/42", "current_entries": ["private-a"]}}]},
            str(settings.project.resolve()), 10)
        result = bridge.rpc("learning_status", {"cwd": str(settings.project)})
        capacity = result["memory_capacity"]
        self.assertEqual(capacity["files"]["MEMORY.md"]["used_chars"], 34)
        self.assertEqual(capacity["files"]["MEMORY.md"]["remaining_chars"], 8)
        self.assertEqual(capacity["project_files"]["MEMORY.md"], {"entries": 1, "entry_chars": 9})
        self.assertEqual(capacity["unassigned_entries"], 1)
        self.assertEqual(capacity["latest_capacity_refusal"]["reason"], "memory_capacity_exceeded")
        self.assertNotIn("private-a", json.dumps(result))
        self.assertNotIn(str((self.root / "b").resolve()), json.dumps(capacity))
        self.assertFalse(result["effective_policy"]["data_admission"]["semantic_evidence_checks"])
        self.assertFalse(result["effective_policy"]["native"]["curator"]["consolidate"])
        self.assertEqual(result["sharing"]["catalog_visible_now"], 2)
        self.assertEqual(result["sharing"]["catalog_visible_with_existing_publications"], 1)
        preview = bridge.rpc("memory_preview", {"cwd": str(settings.project)})
        self.assertEqual([entry["text"] for entry in preview["entries"]], ["private-a"])
        proposal = {"revision": preview["revision"], "replacements": {keys[0]: "short"}}
        params = {"cwd": str(settings.project), "proposals": proposal}
        reduced = bridge.rpc("memory_preview", params)
        self.assertEqual(reduced["estimated_native_chars_saved"], {"MEMORY.md": 4})
        self.assertEqual(bridge.rpc("memory_preview", {"cwd": str(settings.project)}), preview)
        with self.assertRaises(ValueError):
            bridge.rpc("memory_preview", {**params, "proposals": {**proposal, "replacements": {keys[1]: "b"}}})
        with self.assertRaises(ValueError):
            bridge.rpc("memory_preview", {**params, "proposals": {**proposal, "revision": "old"}})

        publication = bridge.rpc("publication_preview", {"cwd": str(settings.project)})
        self.assertEqual(publication["visible_if_checked"], 1)
        self.assertEqual({row["name"]: row["state"] for row in publication["entries"]},
                         {"own": "project_visible", "unpublished": "origin_unknown"})


if __name__ == "__main__":
    unittest.main()
