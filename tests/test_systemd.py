import contextlib
import io
import json
import os
import sys
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.systemd import LABEL, render_unit, systemd


class SystemdTests(unittest.TestCase):
    def test_supervisor_survives_a_missing_worker_interpreter(self):
        from dataclasses import replace
        settings = replace(self.settings, python=self.root / "removed-worker" / "python")
        unit = render_unit(settings, self.path)
        self.assertNotIn(str(settings.python), unit)
        self.assertIn(str(Path(sys.executable).absolute()), unit)
        self.assertIn('"-I" "-B"', unit)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "account"
        self.settings = Settings(self.root / "learning", self.root / "project with spaces",
                                 self.root / "source", Path(sys.executable))
        self.settings.project.mkdir()
        self.settings.home.mkdir()
        self.path = self.settings.home / "bridge.json"
        self.path.write_text(json.dumps({key: str(getattr(self.settings, key))
                                        for key in ("home", "project", "source", "python")}))
        self.target = self.home / ".config/systemd/user" / (LABEL + ".service")

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, **kwargs):
        args = SimpleNamespace(settings=str(self.path), start=False, replace=False, **kwargs)
        return args

    def test_generate_does_not_install_or_start(self):
        with patch("hermes_codex_learning.systemd.Path.home", return_value=self.home), \
             patch("hermes_codex_learning.systemd.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(systemd(self.run_command()), 0)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.settings.state / (LABEL + ".service")).is_file())
        run.assert_not_called()

    def test_start_installs_enables_and_starts_the_named_unit(self):
        args = self.run_command()
        args.start = True
        with patch("hermes_codex_learning.systemd.Path.home", return_value=self.home), \
             patch("hermes_codex_learning.systemd.subprocess.run") as run, \
             patch("hermes_codex_learning.systemd.verify_service") as verify, \
             contextlib.redirect_stdout(io.StringIO()):
            systemd(args)
        verify.assert_called_once()
        self.assertIn("Restart=on-failure", self.target.read_text())
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemd-analyze", "--user", "verify", str(self.settings.state / (LABEL + ".service"))],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", LABEL + ".service"],
        ])

    def test_existing_install_is_not_overwritten_without_replace(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("original")
        args = self.run_command()
        args.start = True
        with patch("hermes_codex_learning.systemd.Path.home", return_value=self.home), \
             patch("hermes_codex_learning.systemd.subprocess.run") as run:
            with self.assertRaises(FileExistsError):
                systemd(args)
        self.assertEqual(self.target.read_text(), "original")
        run.assert_not_called()

    def test_paths_are_quoted_and_systemd_expansion_is_escaped(self):
        root = self.root / 'plugin %n $HOME "quoted"'
        unit = render_unit(self.settings, self.path, plugin_root=root)
        self.assertIn('WorkingDirectory=' + str(self.settings.project) + '\n', unit)
        self.assertIn('plugin %%n $$HOME \\"quoted\\"', unit)
        self.assertIn('Environment="HERMES_HOME=' + str(self.settings.home) + '"', unit)
        self.assertIn("TimeoutStopSec=60s", unit)
        self.assertNotIn("osascript", unit)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "Requires the real systemd unit parser")
    def test_rendered_unit_passes_the_native_parser(self):
        candidate = self.root / (LABEL + ".service")
        candidate.write_text(render_unit(self.settings, self.path))
        runtime = self.root / "xdg-runtime"
        runtime.mkdir(mode=0o700)
        result = subprocess.run(["systemd-analyze", "--user", "verify", str(candidate)], capture_output=True, text=True,
                                env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_line_break_in_path_is_rejected(self):
        with self.assertRaises(ValueError):
            render_unit(self.settings, Path("bad\npath"))


if __name__ == "__main__":
    unittest.main()
