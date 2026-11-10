import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import atomic_json, digest


class RuntimeCleanupTests(unittest.TestCase):
    def test_current_previous_and_fallback_remain_while_old_unreferenced_candidate_is_removed(self):
        from hermes_codex_learning.retention import runtime_cleanup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/"home",root,root/"source",Path("/usr/bin/python3"))
            updates = settings.home.parent/"updates"/digest(str(settings.home))[:16]
            candidates = []
            for index in range(4):
                folder = updates/"candidates"/("v0.23.0-"+f"{index:012x}")
                folder.mkdir(parents=True)
                (folder/"source").mkdir()
                atomic_json(folder/"verification.json",{"id":folder.name,"source":str(folder/"source"),
                            "state":"ready","started_at":time.time()-40*86400})
                candidates.append(folder)
            settings = replace(settings,source=candidates[0]/"source",fallback_python=candidates[1]/"runtime/bin/python")
            atomic_json(updates/"previous.json",{"source":str(candidates[2]/"source")})
            preview = runtime_cleanup(settings,dry_run=True)
            self.assertEqual(preview["eligible_candidates"],1)
            self.assertTrue(all(path.exists() for path in candidates))
            done = runtime_cleanup(settings,dry_run=False)
            self.assertEqual(done["removed_candidates"],1)
            self.assertTrue(all(path.exists() for path in candidates[:3]))
            self.assertFalse(candidates[3].exists())
