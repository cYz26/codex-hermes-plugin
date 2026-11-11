import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReviewSetupTests(unittest.TestCase):
    def test_review_assets_are_private_and_previous_assets_are_recoverable(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "desktop/setup_reviews.py"
        self.assertTrue(script.is_file(), "The Mac review installer is missing")
        spec = importlib.util.spec_from_file_location("review_setup", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state with spaces"
            state.mkdir()
            config = state / "config.json"
            config.write_text(json.dumps({"state": str(state), "host": "fixture", "bridge": "/bridge",
                                          "settings": "/settings", "remote_python": "/usr/bin/python3"}))
            before = config.read_bytes()
            (state / "review.html").write_text("previous view")
            result = module.install_assets(config, root)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual((Path(result["backup"]) / "review.html").read_text(), "previous view")
            self.assertEqual((state / "review.py").read_bytes(), (root / "desktop/review.py").read_bytes())
            self.assertEqual((state / "review.html").read_bytes(), (root / "desktop/review.html").read_bytes())
            launcher = state / "review.command"
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
            self.assertEqual(subprocess.run(["/bin/sh", "-n", str(launcher)], capture_output=True).returncode, 0)
            module.rollback_assets(state, result)
            self.assertEqual((state / "review.html").read_text(), "previous view")
            self.assertFalse((state / "review.py").exists())
            self.assertFalse(launcher.exists())
            self.assertEqual(config.read_bytes(), before)
