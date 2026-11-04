import contextlib
import io
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings


class ProfileAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "learning-data" / "pilot"
        self.home.mkdir(parents=True)
        self.profile = self.root / "account" / ".hermes" / "profiles" / "codex-learning-pilot"
        self.profile.parent.mkdir(parents=True)
        self.profile.symlink_to(self.home, target_is_directory=True)
        self.config = self.home / "bridge.json"
        self.config.write_text(json.dumps({
            "home": str(self.home), "profile_home": str(self.profile),
            "project": str(self.root / "project"), "source": str(self.root / "source"),
            "python": str(self.root / "runtime" / "bin" / "python"),
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_environment_preserves_profile_spelling_and_private_data(self):
        settings = Settings.load(self.config)
        self.assertEqual(settings.home, self.home.resolve())
        self.assertEqual(settings.native_environment()["HERMES_HOME"], str(self.profile))
        self.assertEqual(settings.native_environment()["HERMES_RUNTIME_DIR"], str(self.home.parent / "tools"))
        self.assertEqual(settings.state, self.home / "bridge")
        self.assertFalse((self.home / "auth.json").exists())

    def test_launchd_carries_the_same_profile_as_cli_and_probe(self):
        from hermes_codex_learning.cli import launchd

        with patch("hermes_codex_learning.cli.Path.home", return_value=self.root / "account"):
            with contextlib.redirect_stdout(io.StringIO()):
                launchd(SimpleNamespace(settings=str(self.config), replace=False, start=False))
        path = self.root / "account/Library/LaunchAgents/com.cy.hermes-codex-learning.pilot.plist"
        payload = plistlib.loads(path.read_bytes())
        self.assertEqual(payload["EnvironmentVariables"]["HERMES_HOME"], str(self.profile))
        self.assertEqual(payload["EnvironmentVariables"]["HERMES_RUNTIME_DIR"], str(self.home.parent / "tools"))

    def test_foreign_profile_cannot_redirect_learning_state(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["profile_home"] = str(self.root / "account/.hermes/profiles/someone-else")
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "profile.*home"):
            Settings.load(self.config).native_environment()


if __name__ == "__main__":
    unittest.main()
