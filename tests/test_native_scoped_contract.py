import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NativeScopedContractTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("HERMES_LEARNING_NATIVE_SOURCE"), "Runs in the verified native candidate gate")
    def test_explicit_refine_and_material_batches_use_native_writes_and_publication(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run([sys.executable,"-I","-B",str(root/"probes/scoped_contract.py"),
                "--source",os.environ["HERMES_LEARNING_NATIVE_SOURCE"],"--workspace",str(Path(temporary)/"active"),
                "--explicit"],capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout[-6000:]+result.stderr[-3000:])
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["passed"])

    @unittest.skipUnless(os.environ.get("HERMES_LEARNING_NATIVE_SOURCE"), "Runs in the verified native candidate gate")
    def test_enterprise_scope_shares_internally_and_revokes_on_model_change(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run([sys.executable,"-I","-B",str(root/"probes/scoped_contract.py"),
                                   "--source",os.environ["HERMES_LEARNING_NATIVE_SOURCE"],"--workspace",str(Path(temporary)/"scope"),
                                   "--enterprise"],capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout[-6000:]+result.stderr[-3000:])
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["passed"])

    @unittest.skipUnless(os.environ.get("HERMES_LEARNING_NATIVE_SOURCE"), "Runs in the verified native candidate gate")
    def test_real_native_fork_and_tools_preserve_admission_scope(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)/"scope"
            process = subprocess.run([sys.executable,"-I","-B",str(root/"probes/scoped_contract.py"),
                                      "--source",os.environ["HERMES_LEARNING_NATIVE_SOURCE"],"--workspace",str(workspace)],
                                     capture_output=True,text=True,timeout=120)
            self.assertEqual(process.returncode,0,process.stdout[-6000:]+process.stderr[-3000:])
            result = json.loads(process.stdout.strip().splitlines()[-1])
            self.assertTrue(result["passed"])
