import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.quality import sandbox_backend

class QualityTests(unittest.TestCase):
    def test_new_verifier_rechecks_a_withheld_candidate_with_an_unchanged_catalog(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.daemon import Bridge
        from hermes_codex_learning.store import digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            settings = Settings(root / "home", project, root / "source", Path("/usr/bin/python3"),
                                quality_checks=True, checked_publication=True,
                                data_policy={"synthetic_roots": [str(project)]})
            artifact = settings.home / "learning/artifacts/example"
            artifact.mkdir(parents=True)
            content = "---\nname: learned-example\nlearning_scope: global_candidate\n---\nWhen to Use: notifications.\nLimits: no native GUI claim.\nVerification: fixtures.\n"
            (artifact / "SKILL.md").write_text(content)
            files = {"SKILL.md": hashlib.sha256(content.encode()).hexdigest()}
            value = {"name": "learned-example", "content": content, "resource_dir": str(artifact),
                     "files": files, "version": digest(files)}
            api = SimpleNamespace(read=lambda **kwargs: value)
            bridge = Bridge(settings, api)
            context = {"version": "unchanged", "memories": {}, "skills": [{"name": "learned-example"}],
                       "bridge_provenance": {"skills": {"learned-example": {"projects": [str(project)]}}}}
            try:
                with patch("hermes_codex_learning.quality.verifier_hash", return_value="old-verifier"), \
                     patch("hermes_codex_learning.quality.verify", return_value={"state": "failed", "reason": "missing_applicability_limits_or_verification"}):
                    self.assertEqual(bridge.publish_checked(context)["publications"], {})
                with patch("hermes_codex_learning.quality.verifier_hash", return_value="new-verifier"):
                    self.assertIn("learned-example", bridge.publish_checked(context)["publications"])
            finally:
                bridge.store.close()

    def test_when_to_use_section_satisfies_applicability_contract(self):
        from hermes_codex_learning.quality import verify
        content = (
            "---\nname: learned-native-notifications\n---\n"
            "## When to Use\nRepair native notification senders.\n"
            "## Scope\nActual GUI acceptance is outside its scope.\n"
            "## Verification and reporting\nCheck the actual sender and a fresh visible notification.\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text(content)
            value = {"version": "v1", "resource_dir": directory, "content": content,
                     "files": {"SKILL.md": hashlib.sha256(content.encode()).hexdigest()}}
            proof = verify(value)
            self.assertEqual(proof["state"], "passed", proof)
            self.assertEqual(proof["kind"], "content_contract")

    @unittest.skipUnless(sandbox_backend(), "Executable sandbox is unavailable")
    def test_script_contract_runs_in_isolation_and_proof_binds_version(self):
        from hermes_codex_learning.quality import verify, proof_valid
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"SKILL.md").write_text("---\nname: learned-normalize\nvalidation_suite: normalize-headers-v1\nvalidation_entry: normalize.py\n---\nApplicability: CSV headers.\nLimits: strings only.\nVerification: fixtures.\n")
            (root/"normalize.py").write_text("import json,sys,unicodedata\nprint(json.dumps([unicodedata.normalize('NFKC',x.strip()) for x in json.load(sys.stdin)]))\n")
            files = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
            value = {"name":"learned-normalize","version":"v1","resource_dir":directory,"files":files,
                     "content":(root/"SKILL.md").read_text()}
            proof = verify(value)
            self.assertEqual(proof["state"], "passed", proof)
            self.assertEqual(proof["kind"], "isolated_execution")
            self.assertTrue(proof_valid(value, proof))
            (root/"normalize.py").write_text("raise RuntimeError('changed')")
            self.assertFalse(proof_valid(value, proof))

    @unittest.skipUnless(sandbox_backend(), "Executable sandbox is unavailable")
    def test_program_cannot_read_host_home(self):
        from hermes_codex_learning.quality import verify
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root/"candidate"
            candidate.mkdir()
            secret = root/"private"
            secret.write_text("synthetic-private-marker")
            (candidate/"SKILL.md").write_text("---\nvalidation_suite: normalize-headers-v1\nvalidation_entry: bad.py\n---\nApplicability\nLimits\nVerification\n")
            (candidate/"bad.py").write_text("print(open("+repr(str(secret))+").read())")
            value = {"version":"v1","resource_dir":str(candidate),"files":{
                p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in candidate.iterdir()},
                "content":(candidate/"SKILL.md").read_text()}
            result = verify(value)
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("synthetic-private-marker", json.dumps(result))

    @unittest.skipUnless(sandbox_backend() == "sandbox-exec", "Requires macOS executable sandbox")
    def test_mac_shell_runs_with_no_network_and_no_child_process_contract(self):
        from hermes_codex_learning.quality import verify
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = ("---\nvalidation_suite: normalize-headers-v1\nvalidation_entry: check.sh\n"
                       "---\nApplicability\nLimits\nVerification\n")
            (root / "SKILL.md").write_text(content)
            # A Shell fixture may use builtins, but the confinement probe
            # independently checks that a subprocess cannot be started.
            (root / "check.sh").write_text('IFS= read -r input\ncase "$input" in\n'
                                           '  *ｕｓｅｒ*) printf \'%s\\n\' \'["user_id", "name"]\' ;;\n'
                                           '  *ﬁeld*) printf \'%s\\n\' \'["Å", "field", ""]\' ;;\n'
                                           '  *CaseSensitive*) printf \'%s\\n\' \'["CaseSensitive", "spaced", "1"]\' ;;\n'
                                           '  *) exit 3 ;;\nesac\n')
            files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
            proof = verify({"version": "shell-fixture", "resource_dir": directory, "content": content, "files": files})
            self.assertEqual((proof["state"], proof["backend"]), ("passed", "sandbox-exec"), proof)
