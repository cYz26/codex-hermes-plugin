import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from hermes_codex_learning.adaptation import Adapter, validate_recipe, validate_workspace
from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import atomic_json
from hermes_codex_learning.updates import UpdateError, Updater, file_hash


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

    def test_site_only_adapter_preserves_reviewed_release_and_verifies_site_patch(self):
        commit = "3b421578588243f920886e6fe79a6faa87ea026e"
        for root in (self.trusted, self.workspace):
            tag_dir = root / "patches/v0.23.1"
            tag_dir.mkdir(parents=True, exist_ok=True)
            patch_file = tag_dir / "native-learning-api-v1.patch"
            patch_file.write_text("diff --git a/agent/learning_api.py b/agent/learning_api.py\n")
            manifest = {
                "upstream": "https://github.com/NousResearch/hermes-agent.git",
                "tag": "v0.23.1",
                "base_commit": commit,
                "patch_file": "native-learning-api-v1.patch",
                "patch_sha256": file_hash(patch_file),
                "files": {"agent/learning_api.py": "a" * 64},
                "mechanism_review": {"classification": "adapted", "reason": "shared learning", "agreed_plan": "docs/SHARED-LEARNING.zh-CN.md"},
                "shared_learning_protocol": 1,
            }
            (tag_dir / "manifest.json").write_text(json.dumps(manifest))
            (root / "patches/compatibility.json").write_text(json.dumps({
                "schema": 1, "learning_api": 1, "releases": {"v0.23.1": "v0.23.1/manifest.json"},
                "native_tests": ["tests/native.py"],
            }))
        site_patch = self.workspace / "patches/v0.23.1/site.patch"
        site_patch.write_text("diff --git a/agent/route_binding.py b/agent/route_binding.py\n")
        candidate_manifest = json.loads((self.workspace / "patches/v0.23.1/manifest.json").read_text())
        candidate_manifest["site_patch_file"] = "site.patch"
        candidate_manifest["site_patch_sha256"] = file_hash(site_patch)
        (self.workspace / "patches/v0.23.1/manifest.json").write_text(json.dumps(candidate_manifest))

        recipe = validate_recipe(self.trusted, self.workspace, "v0.23.1", commit)
        self.assertEqual(recipe["site_patch_file"], "site.patch")
        self.assertEqual(recipe["shared_learning_protocol"], 1)

        tampered = {**candidate_manifest, "shared_learning_protocol": 2}
        (self.workspace / "patches/v0.23.1/manifest.json").write_text(json.dumps(tampered))
        with self.assertRaisesRegex(UpdateError, "cannot alter a reviewed upstream learning recipe"):
            validate_recipe(self.trusted, self.workspace, "v0.23.1", commit)
