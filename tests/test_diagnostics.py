import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.diagnostics import inspect


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


if __name__ == "__main__":
    unittest.main()
