import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path


class QualityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bwrap"), "Linux sandbox is unavailable")
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

    @unittest.skipUnless(shutil.which("bwrap"), "Linux sandbox is unavailable")
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
