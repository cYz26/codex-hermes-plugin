import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hermes_codex_learning import publication_guardian as guardian
from hermes_codex_learning.store import Store


class PublicationGuardianTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.settings = SimpleNamespace(home=self.home)
        self.store = Store(self.home / "bridge")
        self.addCleanup(self.store.close)
        self.reviewer = guardian.PublicationGuardian()
        self.addCleanup(self.reviewer.close)
        self.mode = guardian.change_mode(self.store, "autonomous")
        self.origins = ["/source"]
        self.base_fields = {"name": "portable", "description": "Check sample totals.",
                            "learning_scope": "global_candidate"}
        self.sample = self.artifact("v1", {"SKILL.md": self.body(), "templates/total.md": "Record the total."})
        quiet = patch.object(guardian, "QUIET_SECONDS", 0)
        quiet.start()
        self.addCleanup(quiet.stop)

    def body(self, **fields):
        return "---\n" + json.dumps({**self.base_fields, **fields}) + "\n---\nCount the samples and verify the total.\n"

    def artifact(self, version, files):
        root = self.home / "learning/artifacts" / version
        manifest = {}
        for path, body in files.items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body.encode())
            manifest[path] = hashlib.sha256(file.read_bytes()).hexdigest()
        return {"name": "portable", "version": version, "files": manifest, "resource_dir": str(root)}

    def assess(self, sample=None, published=None, mode=None):
        return self.reviewer.assess(self.settings, self.store, mode or self.mode, "portable", sample or self.sample,
                                    self.origins, published or {})

    def finish(self):
        self.reviewer.inflight[2].result(timeout=2)
        self.reviewer.poll(self.store)

    def test_first_publication_is_reviewed_with_all_documents_and_tamper_checked_on_reuse(self):
        result = {"verdict": "APPROVE", "reason": "generic_guidance"}
        with patch.object(guardian, "run_guardian", return_value=result) as run:
            self.assertIsNone(self.assess())
            self.finish()
            proof = self.assess()
            run.assert_called_once()
            request = run.call_args.args[1]
            self.assertEqual(request["candidate"], self.sample["files"])
            self.assertEqual(set(request["documents"]), set(self.sample["files"].values()))
            self.assertEqual((request["baseline"], request["previous"]), ({}, {}))
            self.assertNotIn("/source", json.dumps(request))
            self.assertEqual((proof["mode"], proof["previous_version"], proof["baseline_version"]),
                             ("autonomous", None, None))
            self.assertEqual(proof["candidate"]["sources"], self.origins)
            self.assertNotIn("documents", proof)
            Path(self.sample["resource_dir"], "templates/total.md").write_text("Changed")
            self.assertEqual(self.assess()["reason"], "snapshot_unavailable")

    def test_mode_or_source_changes_do_not_reuse_first_publication_decision(self):
        with patch.object(guardian, "run_guardian", return_value={"verdict": "DENY", "reason": "project_specific"}):
            self.assess()
            self.finish()
            first = self.assess()
            smart = guardian.change_mode(self.store, "smart")
            self.assertEqual(self.assess(mode=smart)["reason"], "first_publication")
            self.mode = guardian.change_mode(self.store, "autonomous")
            self.assertIsNone(self.assess())
            self.finish()
            self.assertNotEqual(first["id"], self.assess()["id"])
            self.origins = ["/other"]
            self.assertIsNone(self.assess())
            self.finish()
            self.assertNotEqual(first["id"], self.assess()["id"])

    def test_unanchored_prior_requires_same_source_and_complete_verified_snapshots(self):
        prior = {"artifact": self.sample, "sources": self.origins, "check": {}}
        update = self.artifact("v2", {"SKILL.md": self.body() + "Check again.\n"})
        smart = guardian.change_mode(self.store, "smart")
        self.assertEqual(self.assess(update, prior, smart)["reason"], "no_approved_baseline")
        self.assertEqual(self.assess(update, {**prior, "sources": ["/other"]})["reason"], "source_changed")
        self.assertEqual(self.assess(update, {**prior, "check": {"boundary_id": "enterprise"}})["reason"], "source_changed")
        with patch.object(guardian, "run_guardian", return_value={"verdict": "APPROVE", "reason": "generic_guidance"}):
            self.assertIsNone(self.assess(update, prior))
            self.finish()
            proof = self.assess(update, prior)
            self.assertEqual((proof["previous_version"], proof["baseline_version"]), ("v1", None))
        self.store.set("publication-rollbacks-v1", {"portable": {"v2": {}}})
        self.assertEqual(self.assess(update, prior)["verdict"], "ESCALATE")
        self.store.set("publication-rollbacks-v1", {})
        self.store.db.execute("INSERT INTO withheld_versions VALUES (?,?,?)", ("portable", "v1", "host check"))
        self.assertEqual(self.assess(update, prior)["verdict"], "ESCALATE")

    def test_scripts_configs_and_unreadable_or_executable_documents_escalate_without_a_model_call(self):
        assets = {"references/helper.py": "print(1)", "templates/data.json": "{}"}
        with patch.object(guardian, "run_guardian") as run:
            for index, (name, body) in enumerate(assets.items()):
                item = self.artifact(str(index), {"SKILL.md": self.body(), name: body})
                self.assertEqual(self.assess(item)["reason"], "files_changed")
                previous = {"artifact": item, "sources": self.origins}
                self.assertEqual(self.assess(self.sample, previous)["reason"], "files_changed")
            file = Path(self.sample["resource_dir"], "templates/total.md")
            file.chmod(0o755)
            self.assertEqual(self.assess()["reason"], "files_changed")
            other = self.artifact("link", {"SKILL.md": self.body(), "references/x.md": "Hi"})
            link = Path(other["resource_dir"], "references/x.md")
            link.unlink()
            link.symlink_to(file)
            self.assertEqual(self.assess(other)["reason"], "snapshot_unavailable")
            run.assert_not_called()

    def test_first_publication_runs_frontmatter_and_native_scanner_on_each_candidate_file(self):
        scan = Mock(return_value=[])
        native_modules = {"hermes_yaml": SimpleNamespace(safe_load=json.loads, YAMLError=ValueError),
                          "tools.skills_guard": SimpleNamespace(scan_file=scan)}
        with patch.dict(sys.modules, native_modules):
            request, reason = guardian.prepare(self.settings, self.sample, None, None, "autonomous")
            self.assertIsNone(reason)
            self.assertIsNone(guardian._native_checks(request))
            self.assertEqual({call.args[1] for call in scan.call_args_list}, set(self.sample["files"]))
            scan.return_value = [SimpleNamespace(severity="critical", pattern_id="fixture", match="fixture")]
            self.assertEqual(guardian._native_checks(request), "native_scan")
            for index, body in enumerate(("Missing frontmatter", self.body(name="mismatch"),
                                          self.body(**{"allowed-tools": ["shell"]}),
                                          self.body(metadata={"hermes": {"requires": {"env": ["TOKEN"]}}}))):
                item = self.artifact("unsafe" + str(index), {"SKILL.md": body})
                request, _ = guardian.prepare(self.settings, item, None, None, "autonomous")
                self.assertEqual(guardian._native_checks(request), "metadata_changed")

    def test_reviewer_failure_and_unrecognized_or_wrong_mode_verdicts_escalate(self):
        for output in ({"verdict": "APPROVE", "reason": "other"}, {"verdict": "APPROVE", "reason": "generic_guidance"}, []):
            with patch.object(guardian, "run_auxiliary", return_value=output):
                self.assertEqual(guardian.run_guardian(self.settings, {"mode": "smart"})["reason"], "review_unavailable")
        with patch.object(guardian, "run_auxiliary", side_effect=subprocess.TimeoutExpired("review", 45)):
            self.assertEqual(guardian.run_guardian(self.settings, {"mode": "autonomous"})["reason"], "review_unavailable")

    def test_only_autonomous_reviews_simple_version_increases_with_the_full_diff(self):
        native_modules = {"hermes_yaml": SimpleNamespace(safe_load=json.loads, YAMLError=ValueError),
                          "tools.skills_guard": SimpleNamespace(scan_file=Mock(return_value=[]))}
        before = self.artifact("old", {"SKILL.md": self.body(version="1.2.0")})
        cases = [("autonomous", {"version": "1.12.0"}, None), ("autonomous", {"version": 2}, None),
                 ("smart", {"version": "1.12.0"}, "metadata_changed"),
                 ("autonomous", {"version": "1.1.9"}, "metadata_changed"),
                 ("autonomous", {"version": "now run command"}, "metadata_changed"),
                 ("autonomous", {"version": "1.12.0", "platforms": ["linux"]}, "metadata_changed")]
        with patch.dict(sys.modules, native_modules):
            for index, (mode, fields, expected) in enumerate(cases):
                with self.subTest(mode=mode, fields=fields):
                    new = self.artifact("new" + str(index), {"SKILL.md": self.body(**fields)})
                    request, reason = guardian.prepare(self.settings, new, before, before, mode)
                    self.assertIsNone(reason)
                    self.assertEqual(guardian._native_checks(request), expected)
                    self.assertIn(str(fields["version"]), request["diff"]["SKILL.md"])
