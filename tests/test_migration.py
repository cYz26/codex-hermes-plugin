import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.migration import prepare


class MigrationTests(unittest.TestCase):
    def test_local_edit_is_ported_without_editing_running_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, official = root / "old", root / "official"
            source.mkdir()
            def git(*args):
                subprocess.run(["git", *map(str, args)], check=True, capture_output=True)
            git("init", source)
            (source / "app.py").write_text("setting = 1\n")
            git("-C", source, "add", ".")
            git("-C", source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
            git("clone", source, official)
            (source / "app.py").write_text("setting = 2\n")
            result = prepare(source, official, root / "migration")
            self.assertEqual(result["state"], "prepared")
            self.assertEqual((Path(result["candidate"]) / "app.py").read_text(), "setting = 2\n")
            self.assertEqual((source / "app.py").read_text(), "setting = 2\n")
            self.assertEqual((official / "app.py").read_text(), "setting = 1\n")
            self.assertFalse(result["activated"])
