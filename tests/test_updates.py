import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import atomic_json, digest
from hermes_codex_learning.updates import (UpdateError, Updater, assess_changes, catalog, exclusive,
                                          file_hash, review_gate, source_seal, version)
from hermes_codex_learning.supervisor import Supervisor, restore, snapshot
from hermes_codex_learning.official_runtime import verify_update_target


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.settings = Settings(self.home, self.root, self.root / "old", Path(sys.executable))
        self.updater = Updater(self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def test_numeric_stable_versions_and_no_command_or_path_in_tag(self):
        self.assertGreater(version("v0.23.10"), version("v0.23.9"))
        for value in ("v0.23.0-rc.1", "main", "../v0.23.0", "v0.23.0;exit", "v01.2.3", None):
            with self.assertRaises(UpdateError):
                version(value)

    def test_policy_rejects_invalid_modes_and_nonfinite_schedule(self):
        for fields in ({"update_mode": "force"}, {"update_interval": 0}, {"update_interval": float("nan")}):
            with self.assertRaises(ValueError):
                replace(self.settings, **fields)

    def test_unknown_release_with_large_learning_changes_requests_a_plan(self):
        release = {"tag": "v0.99.0"}
        comparison = {"files": [{"filename": "agent/background_review.py", "status": "modified",
                                  "additions": 180, "deletions": 90}]}
        with patch.object(Updater, "current", return_value="v0.22.7"):
            result = self.updater.check(fetch=lambda _: release, compare=lambda *_: comparison)
        self.assertEqual(result["state"], "plan_required")
        self.assertTrue(result["assessment"]["changed_mechanisms"])
        self.assertFalse((self.updater.directory / "request.json").exists())

    def test_truncated_comparison_cannot_qualify_as_no_mechanism_changes(self):
        comparison = {"files": [{"filename": f"docs/{i}", "status": "modified"} for i in range(300)]}
        result = assess_changes(comparison, current="v0.22.7", target="v0.23.0")
        self.assertEqual(result["state"], "mechanism_review_required")
        self.assertTrue(result["comparison_may_be_truncated"])

    def test_renamed_mechanism_is_not_hidden_by_its_new_path(self):
        result = assess_changes({"files": [{"filename": "new/engine.py", "previous_filename": "agent/curator.py",
                                           "status": "renamed", "additions": 0, "deletions": 0}]},
                                current="v0.22.7", target="v0.23.0")
        self.assertEqual(result["state"], "plan_required")

    def test_redesign_recipe_is_not_allowed_before_plan_agreement(self):
        recipe = {"tag": "v0.23.0", "mechanism_review": {"classification": "redesign_required", "reason": "new flow"}}
        with self.assertRaises(UpdateError):
            review_gate(recipe)
        recipe["mechanism_review"]["classification"] = "adapted"
        with self.assertRaises(UpdateError):
            review_gate(recipe)
        recipe["mechanism_review"]["agreed_plan"] = "docs/user-agreed-migration.md"
        review_gate(recipe)

    def test_network_failure_keeps_previous_success_and_live_settings(self):
        atomic_json(self.updater.status_path, {"last_successful_check": 42, "state": "current"})
        marker = self.home / "bridge.json"
        marker.write_text("unchanged")
        with patch.object(Updater, "current", return_value="v0.22.7"):
            result = self.updater.check(fetch=lambda _: (_ for _ in ()).throw(TimeoutError("offline")))
        self.assertEqual(result["state"], "check_failed")
        self.assertEqual(result["last_successful_check"], 42)
        self.assertEqual(marker.read_text(), "unchanged")

    def test_single_owner_and_unknown_release_do_not_create_candidates(self):
        with exclusive(self.updater.lock):
            with self.assertRaises(UpdateError):
                self.updater.check()
        with self.assertRaises(UpdateError):
            self.updater.prepare("v99.0.0")
        self.assertFalse((self.updater.directory / "candidates").exists())

    def test_plan_required_never_stages_or_activates_in_auto_mode(self):
        with patch.object(self.updater, "check", return_value={"state": "plan_required"}), \
                patch.object(self.updater, "prepare") as prepare, patch.object(self.updater, "request") as request:
            self.updater.cycle()
        prepare.assert_not_called()
        request.assert_not_called()

    def test_check_mode_does_not_build_and_failed_candidate_does_not_activate(self):
        available = {"state": "available", "latest": {"tag": "v0.23.0"}}
        updater = Updater(replace(self.settings, update_mode="check"))
        with patch.object(updater, "check", return_value=available), patch.object(updater, "prepare") as prepare:
            updater.cycle()
            prepare.assert_not_called()
        with patch.object(self.updater, "check", return_value=available), \
                patch.object(self.updater, "prepare", return_value={"state": "failed"}), patch.object(self.updater, "request") as request:
            self.updater.cycle()
            request.assert_not_called()

    def test_official_app_change_can_be_followed_without_fetching_a_release(self):
        updater = Updater(replace(self.settings, official_source=self.root / "hermes-agent"))
        commit = catalog()["v0.23.0"]["base_commit"]
        with patch.object(updater, "current", return_value="v0.22.7"), \
                patch("hermes_codex_learning.updates.git", return_value=commit), \
                patch("hermes_codex_learning.official_runtime.inspect_official",
                      return_value={"python": "/official/venv/bin/python", "stamp": "published"}):
            result = updater.check(installed_only=True, fetch=lambda _: self.fail("unexpected network check"))
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["target"], "v0.23.0")
        self.assertEqual(result["official"]["stamp"], "published")

    def test_native_installation_waits_for_dependency_publication(self):
        updater = Updater(replace(self.settings, official_source=self.root / "hermes-agent"))
        commit = catalog()["v0.23.0"]["base_commit"]
        with patch.object(updater, "current", return_value="v0.22.7"), \
                patch("hermes_codex_learning.updates.git", return_value=commit), \
                patch("hermes_codex_learning.official_runtime.inspect_official",
                      side_effect=UpdateError("dependencies are not current")), patch.object(updater, "prepare") as prepare:
            result = updater.cycle(installed_only=True)
        self.assertEqual(result["state"], "official_pending")
        prepare.assert_not_called()

    def test_official_upgrade_preflight_cannot_be_activated_as_a_worker(self):
        atomic_json(self.updater.directory / "candidate.json", {"id": "probe", "preflight_only": True})
        with patch.object(self.updater, "validate_candidate"):
            with self.assertRaisesRegex(UpdateError, "Preflight"):
                self.updater.request("apply")
        self.assertFalse((self.updater.directory / "request.json").exists())

    def test_native_upgrade_requires_preflight_then_follows_the_published_install(self):
        updater = Updater(replace(self.settings, official_source=self.root / "hermes-agent", upgrade_official=True))
        config = self.home / "bridge.json"
        order = []
        def prepare(tag, **kwargs):
            order.append("preflight" if kwargs.get("preflight") else "shared-verification")
            return {"state": "ready", "tag": tag, **kwargs}
        def advance(candidate, before, path):
            self.assertTrue(candidate["preflight"])
            self.assertEqual(path, config)
            order.append("native-publish")
            return {"state": "published"}
        waiting = {"state": "waiting_for_official", "target": "v0.23.0", "latest": {"tag": "v0.23.0"},
                   "official": {"commit": "older"}}
        local = {"state": "available", "target": "v0.23.0", "latest": {"tag": "v0.23.0"}}
        with patch.object(updater, "check", side_effect=[waiting, local]), \
                patch.object(updater, "prepare", side_effect=prepare), \
                patch.object(updater, "advance_official", side_effect=advance), \
                patch.object(updater, "request", side_effect=lambda *_a, **_kw: order.append("queue-activation")):
            updater.cycle(config=config)
        self.assertEqual(order, ["preflight", "native-publish", "shared-verification", "queue-activation"])

    def test_official_resolver_must_select_the_verified_stable_commit(self):
        approved = dict(channel="stable", repository="NousResearch/hermes-agent", ahead=False,
                        commit="approved", version="0.23.0")
        target = SimpleNamespace(**approved)
        self.assertIs(verify_update_target(target, "v0.23.0", "approved"), target)
        for drift in ({"commit": "new-release"}, {"version": "0.24.0"}, {"channel": "main"},
                      {"repository": "someone/hermes-agent"}, {"ahead": True}):
            with self.assertRaises(ValueError):
                verify_update_target(SimpleNamespace(**{**approved, **drift}), "v0.23.0", "approved")

    def test_missing_official_generation_uses_verified_private_interpreter(self):
        shared = self.root / "native" / "bin" / "python"
        shared.parent.mkdir(parents=True)
        shared.symlink_to(sys.executable)
        settings = replace(self.settings, python=shared, fallback_python=Path(sys.executable))
        self.assertTrue(settings.borrows_official_python)
        shared.unlink()
        self.assertFalse(settings.borrows_official_python)
        self.assertEqual(settings.interpreter, Path(sys.executable))

    def test_candidate_environment_withholds_provider_and_python_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "PYTHONPATH": "/injected", "HERMES_HOME": "/live"}):
            env = self.updater.environment(self.root / "fixture")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["HERMES_HOME"], str(self.root / "fixture"))

    def test_status_does_not_export_private_settings_from_transaction(self):
        atomic_json(self.updater.directory / "transaction.json", {
            "state": "committed", "before": {"network_environment": {"HTTPS_PROXY": "private-credential"}},
            "after": {"secret": "private-credential"}})
        self.assertNotIn("private-credential", json.dumps(self.updater.status()))

    def test_source_seal_detects_unrelated_edits_and_new_modules(self):
        source = self.root / "source"
        source.mkdir()
        def command(*args):
            return subprocess.check_output(["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL).strip()
        command("init")
        (source / "api.py").write_text("version = 1\n")
        (source / "other.py").write_text("original\n")
        command("add", ".")
        command("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "upstream")
        recipe = {"base_commit": command("rev-parse", "HEAD"), "files": {"api.py": file_hash(source / "api.py")}}
        source_seal(source, recipe)
        (source / "other.py").write_text("changed\n")
        with self.assertRaises(UpdateError):
            source_seal(source, recipe)
        command("restore", "other.py")
        (source / "unexpected.py").write_text("new module\n")
        with self.assertRaises(UpdateError):
            source_seal(source, recipe)

    def test_catalog_checks_real_patch_bytes(self):
        self.assertIn("v0.22.7", catalog())

    def test_candidate_verification_expires_when_plugin_or_source_changes(self):
        folder = self.updater.directory / "candidates/v0.23.0-fixture"
        source = folder / "source"
        source.mkdir(parents=True)
        def command(*args):
            return subprocess.check_output(["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL).strip()
        command("init")
        (source / "api.py").write_text("version=1\n")
        (source / "unrelated.py").write_text("original\n")
        command("add", ".")
        command("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "upstream")
        runtime = folder / "runtime"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python").symlink_to(sys.executable)
        (runtime / "pyvenv.cfg").write_text("fixture\n")
        recipe = {"tag": "v0.23.0", "recipe": "recipe-id", "base_commit": command("rev-parse", "HEAD"),
                  "files": {"api.py": file_hash(source / "api.py")},
                  "mechanism_review": {"classification": "compatible", "reason": "fixture"}}
        record = {"id": folder.name, "tag": "v0.23.0", "recipe": "recipe-id", "state": "ready", "plugin": "current-plugin",
                  "source": str(source), "python": str(runtime / "bin/python"), "source_tree": source_seal(source, recipe),
                  "python_config": file_hash(runtime / "pyvenv.cfg"),
                  "gates": ["native-contracts", "bridge-contracts", "runtime-smoke"]}
        with patch("hermes_codex_learning.updates.catalog", return_value={"v0.23.0": recipe}), \
                patch("hermes_codex_learning.updates.plugin_hash", return_value="current-plugin"):
            self.updater.validate_candidate(record)
            with self.assertRaises(UpdateError):
                self.updater.validate_candidate({**record, "plugin": "old-plugin"})
            with self.assertRaises(UpdateError):
                self.updater.validate_candidate({**record, "gates": []})
            (source / "unrelated.py").write_text("post-verification-edit\n")
            with self.assertRaises(UpdateError):
                self.updater.validate_candidate(record)

    def test_paused_candidate_does_not_publish_or_issue_receipts_before_commit(self):
        from hermes_codex_learning.daemon import Bridge
        class API:
            version = 1
            def read(self, *, kind, **kwargs):
                return {"version": 1, "intents": [], "attempts": []} if kind == "status" else {
                    "version": "native-context", "memories": {}, "skills": []}
        bridge = Bridge(self.settings, API())
        bridge.resume.clear()
        bridge.stop.wait = lambda _: bridge.stop.set()
        try:
            bridge.loop()
            self.assertTrue(bridge.ready.is_set())
            self.assertFalse((self.settings.state / "context.json").exists())
            self.assertEqual(bridge.rpc("learning_status", {})["snapshot"]["version"], 1)
            with self.assertRaises(RuntimeError):
                bridge.rpc("skill_load", {"name": "fixture"})
        finally:
            bridge.store.close()

    def test_update_drain_finishes_current_review_without_starting_next(self):
        from hermes_codex_learning.daemon import Bridge
        calls = []
        class API:
            def read(self, **kwargs):
                return {"intents": [{"id": "i1", "handle": "h1", "status": "queued"},
                                    {"id": "i2", "handle": "h2", "status": "queued"}]}
            def run_review(self, handle, intent, **kwargs):
                calls.append(intent)
                bridge.rpc("quiesce_for_update", {})
                return {"run_status": "completed"}
        bridge = Bridge(self.settings, API())
        try:
            bridge.reviews()
            self.assertEqual(calls, ["i1"])
        finally:
            bridge.store.close()


class FakeWorker:
    def __init__(self, home, failed_source=None):
        self.home, self.failed_source = home, failed_source
        self.starts, self.resumed = [], False

    def drain(self):
        pass

    def start(self, settings, config, *, paused=False):
        self.starts.append((settings.source, paused))
        if settings.source == self.failed_source:
            (self.home / "memories/MEMORY.md").write_text("incompatible startup migration")
            (self.home / "bridge/new-hook.json").write_text("captured during the update")
            (self.home / "auth.json").write_text("rotated-current-credential")

    def ready(self):
        if self.starts[-1][0] == self.failed_source:
            raise UpdateError("new worker fails API checks")

    def stop(self):
        pass

    def resume(self):
        self.resumed = True


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        (self.home / "memories").mkdir(parents=True)
        (self.home / "bridge").mkdir()
        (self.home / "memories/MEMORY.md").write_text("existing learning")
        self.config = self.home / "bridge.json"
        self.before = {"home": str(self.home), "project": str(self.root), "source": str(self.root / "old"),
                       "python": sys.executable, "notifications": False}
        atomic_json(self.config, self.before)
        self.updater = Updater(Settings.load(self.config))
        self.candidate = {"id": "v0.23.0-fixture", "tag": "v0.23.0", "state": "ready",
                          "source": str(self.root / "new"), "python": sys.executable}
        atomic_json(self.updater.directory / "candidate.json", self.candidate)
        atomic_json(self.updater.directory / "request.json", {"id": "r1", "action": "apply", "target": self.candidate["id"]})
        self.verification = patch.object(Updater, "validate_candidate")
        self.current = patch.object(Updater, "current", return_value="v0.22.7")
        self.verification.start()
        self.current.start()

    def tearDown(self):
        self.verification.stop()
        self.current.stop()
        self.temp.cleanup()

    def test_success_commits_before_resume_and_keeps_existing_learning(self):
        worker = FakeWorker(self.home)
        supervisor = Supervisor(self.config, worker=worker)
        supervisor.activate()
        transaction = json.loads((self.updater.directory / "transaction.json").read_text())
        self.assertEqual(transaction["state"], "committed")
        self.assertEqual(worker.starts, [(self.root / "new", True)])
        self.assertTrue(worker.resumed)
        self.assertEqual(json.loads(self.config.read_text())["source"], self.candidate["source"])
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "existing learning")

    def test_failed_startup_restores_learning_but_preserves_new_hooks_and_credentials(self):
        worker = FakeWorker(self.home, self.root / "new")
        Supervisor(self.config, worker=worker).activate()
        self.assertEqual(json.loads(self.config.read_text()), self.before)
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "existing learning")
        self.assertEqual((self.home / "bridge/new-hook.json").read_text(), "captured during the update")
        self.assertEqual((self.home / "auth.json").read_text(), "rotated-current-credential")
        self.assertEqual(worker.starts[-1], (self.root / "old", False))
        self.assertFalse(worker.resumed)
        self.assertEqual(json.loads((self.updater.directory / "transaction.json").read_text())["state"], "rolled_back")

    def test_crash_during_startup_recovers_old_runtime_without_losing_hooks(self):
        backup = self.root / "snapshot"
        snapshot(Settings.load(self.config), backup)
        after = {**self.before, "source": self.candidate["source"]}
        atomic_json(self.updater.directory / "transaction.json", {
            "id": "r1", "state": "starting", "before": self.before, "after": after, "snapshot": str(backup)})
        atomic_json(self.config, after)
        (self.home / "memories/MEMORY.md").write_text("failed migration")
        (self.home / "bridge/late-hook").write_text("late")
        Supervisor(self.config, worker=FakeWorker(self.home)).recover()
        self.assertEqual(json.loads(self.config.read_text()), self.before)
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "existing learning")
        self.assertEqual((self.home / "bridge/late-hook").read_text(), "late")

    def test_committed_update_is_not_restored_over_new_learning_on_restart(self):
        worker = FakeWorker(self.home)
        supervisor = Supervisor(self.config, worker=worker)
        supervisor.activate()
        (self.home / "memories/MEMORY.md").write_text("newly learned after activation")
        supervisor.recover()
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "newly learned after activation")
        self.assertEqual(json.loads(self.config.read_text())["source"], self.candidate["source"])

    def test_concurrent_settings_edit_after_drain_is_preserved(self):
        worker = FakeWorker(self.home)
        changed = {**self.before, "notifications": True}
        worker.drain = lambda: atomic_json(self.config, changed)
        with self.assertRaises(UpdateError):
            Supervisor(self.config, worker=worker).activate()
        self.assertEqual(json.loads(self.config.read_text()), changed)
        self.assertFalse(worker.starts)

    def test_busy_review_is_not_cancelled_or_snapshotted(self):
        worker = FakeWorker(self.home)
        def busy():
            raise UpdateError("review still running")
        worker.drain = busy
        with self.assertRaises(UpdateError):
            Supervisor(self.config, worker=worker).activate()
        self.assertEqual(json.loads(self.config.read_text()), self.before)
        self.assertFalse((self.updater.directory / "transaction.json").exists())

    def test_rollback_keeps_learning_acquired_after_successful_upgrade(self):
        worker = FakeWorker(self.home)
        supervisor = Supervisor(self.config, worker=worker)
        supervisor.activate()
        previous = json.loads((self.updater.directory / "previous.json").read_text())
        atomic_json(self.updater.directory / "request.json", {"id": "r2", "action": "rollback", "target": digest(previous)})
        (self.home / "memories/MEMORY.md").write_text("new knowledge")
        with patch("hermes_codex_learning.supervisor.verify_source"):
            supervisor.activate()
        self.assertEqual(json.loads(self.config.read_text())["source"], self.before["source"])
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "new knowledge")

    def test_snapshot_refuses_a_learning_directory_symlink_outside_home(self):
        external = self.root / "external"
        external.mkdir()
        (self.home / "skills").symlink_to(external, target_is_directory=True)
        with self.assertRaises(UpdateError):
            snapshot(Settings.load(self.config), self.root / "backup")
        self.assertFalse((self.root / "backup/complete.json").exists())

    def test_policy_change_cancels_automatic_activation_but_does_not_modify_runtime(self):
        atomic_json(self.config, {**self.before, "updates": {"mode": "check"}})
        atomic_json(self.updater.directory / "request.json", {"id": "r1", "action": "apply", "automatic": True,
                                                              "target": self.candidate["id"]})
        worker = FakeWorker(self.home)
        Supervisor(self.config, worker=worker).activate()
        self.assertFalse(worker.starts)
        self.assertEqual(json.loads(self.config.read_text())["source"], self.before["source"])
        self.assertFalse((self.updater.directory / "request.json").exists())

    def test_restore_preserves_the_native_owner_lock_inode(self):
        backup = self.root / "snapshot"
        snapshot(Settings.load(self.config), backup)
        owner = self.home / "learning/owner.lock"
        inode = owner.stat().st_ino
        restore(Settings.load(self.config), backup)
        self.assertEqual(owner.stat().st_ino, inode)

    def test_lost_resume_after_commit_restarts_new_runtime_without_restoring_data(self):
        worker = FakeWorker(self.home)
        def lost_resume():
            (self.home / "memories/MEMORY.md").write_text("post-commit learning")
            raise OSError("lost resume response")
        worker.resume = lost_resume
        Supervisor(self.config, worker=worker).activate()
        self.assertEqual(worker.starts[-1], (self.root / "new", False))
        self.assertEqual((self.home / "memories/MEMORY.md").read_text(), "post-commit learning")
        self.assertEqual(json.loads((self.updater.directory / "transaction.json").read_text())["state"], "committed")


if __name__ == "__main__":
    unittest.main()
