import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from hermes_codex_learning.adaptation import Adapter, validate_workspace
from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import atomic_json
from hermes_codex_learning.updates import UpdateError, Updater


class AdaptationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trusted, self.workspace = self.root / "trusted", self.root / "candidate"
        self.tag = "v0.24.0"
        for root in (self.trusted, self.workspace):
            (root / "patches").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "tests/test_contract.py").write_text("assert actual() == expected\n")
            (root / "patches/compatibility.json").write_text(json.dumps({
                "schema": 1, "learning_api": 1, "releases": {}, "native_tests": ["tests/native.py"]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_cannot_weaken_verifier_tests(self):
        (self.workspace / "tests/test_contract.py").write_text("assert True\n")
        with self.assertRaisesRegex(UpdateError, "protected"):
            validate_workspace(self.trusted, self.workspace, self.tag)

    def test_candidate_cannot_remove_required_native_contracts(self):
        path = self.workspace / "patches/compatibility.json"
        data = json.loads(path.read_text())
        data["native_tests"] = []
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(UpdateError, "native_tests"):
            validate_workspace(self.trusted, self.workspace, self.tag)

    def test_new_adapter_cannot_be_a_symlink_outside_workspace(self):
        (self.workspace / "patches" / self.tag).symlink_to(self.trusted, target_is_directory=True)
        with self.assertRaisesRegex(UpdateError, "symlink"):
            validate_workspace(self.trusted, self.workspace, self.tag)

    def updater(self):
        root = self.trusted
        for relative in ("pyproject.toml", ".codex-plugin/plugin.json", ".mcp.json"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        home = self.root / "home"
        home.mkdir()
        return Updater(Settings(home, self.root, self.root / "source", Path("/usr/bin/python3")), root=root)

    def test_redesign_stops_before_any_verification_or_activation(self):
        updater = self.updater()
        adapter = Adapter(updater)
        with patch("hermes_codex_learning.adaptation.official_commit", return_value="b" * 40), \
             patch.object(updater, "current", return_value="v0.23.0"), \
             patch.object(updater, "recipes", return_value={"v0.23.0": {"base_commit": "a" * 40}}), \
             patch.object(adapter, "sources", return_value=self.root / "source"), \
             patch.object(adapter, "invoke", return_value={"classification": "plan_required", "reason": "storage migration"}) as invoke, \
             patch.object(updater, "prepare") as prepare:
            result = adapter.run(self.tag)
            self.assertEqual(result["state"], "plan_required")
            self.assertEqual(adapter.run(self.tag)["state"], "plan_required")
        self.assertEqual(invoke.call_count, 1)
        prepare.assert_not_called()
        self.assertFalse((updater.directory / "request.json").exists())

    def test_independent_verification_failures_stop_after_two_attempts(self):
        updater = self.updater()
        adapter = Adapter(updater)
        with patch("hermes_codex_learning.adaptation.official_commit", return_value="b" * 40), \
             patch.object(updater, "current", return_value="v0.23.0"), \
             patch.object(updater, "recipes", return_value={"v0.23.0": {"base_commit": "a" * 40}}), \
             patch.object(adapter, "sources", return_value=self.root / "source"), \
             patch.object(adapter, "invoke", return_value={"classification": "compatible", "reason": "routine"}) as invoke, \
             patch("hermes_codex_learning.adaptation.validate_recipe", return_value={"recipe": "fixture"}), \
             patch("hermes_codex_learning.adaptation.verify_updater_contract"), \
             patch.object(updater, "prepare", return_value={"state": "failed", "error": "native regression"}) as verify:
            result = adapter.run(self.tag)
            self.assertEqual(result["state"], "adaptation_failed")
            self.assertEqual(result["attempts"], 2)
            adapter.run(self.tag)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        self.assertFalse((updater.directory / "request.json").exists())
