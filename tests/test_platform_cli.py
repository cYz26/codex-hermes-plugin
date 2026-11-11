import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from hermes_codex_learning.cli import main


class PlatformCLITests(unittest.TestCase):
    def test_foreign_platform_activation_is_rejected_before_loading_configuration(self):
        cases = [
            ("linux", ["launchd", "--settings", "/missing/settings.json"], "macOS"),
            ("linux", ["launchd", "--settings", "/missing/settings.json", "--start"], "macOS"),
            ("darwin", ["systemd", "--settings", "/missing/settings.json", "--start"], "Linux"),
            ("linux", ["install-notifier"], "macOS"),
        ]
        for platform, arguments, expected in cases:
            with self.subTest(platform=platform, command=arguments[0]):
                errors = io.StringIO()
                with patch.object(sys, "platform", platform), patch.object(sys, "argv", ["bridge", *arguments]), \
                        patch("hermes_codex_learning.cli.Settings.load") as load, \
                        patch("hermes_codex_learning.cli.subprocess.run") as process, \
                        patch("hermes_codex_learning.cli.launchd", return_value=0) as launchd, \
                        patch("hermes_codex_learning.systemd.systemd", return_value=0) as systemd, \
                        patch("hermes_codex_learning.cli.install_notifier", return_value=0) as notifier, \
                        contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as exit_status:
                        main()
                self.assertEqual(exit_status.exception.code, 2)
                self.assertIn(expected, errors.getvalue())
                load.assert_not_called()
                process.assert_not_called()
                launchd.assert_not_called()
                systemd.assert_not_called()
                notifier.assert_not_called()

    def test_linux_service_preview_remains_available_on_macos(self):
        for start in (False, True):
            arguments = ["bridge", "systemd", "--settings", "/fixture/settings.json", "--dry-run"]
            if start:
                arguments.append("--start")
            with self.subTest(start=start), patch.object(sys, "platform", "darwin"), \
                    patch.object(sys, "argv", arguments), \
                    patch("hermes_codex_learning.systemd.systemd", return_value=0) as render:
                self.assertEqual(main(), 0)
            self.assertTrue(render.call_args.args[0].dry_run)
            self.assertEqual(render.call_args.args[0].start, start)

    def test_native_platform_commands_keep_their_own_adapters(self):
        cases = [("linux", "systemd", "hermes_codex_learning.systemd.systemd"),
                 ("darwin", "launchd", "hermes_codex_learning.cli.launchd")]
        for platform, command, adapter in cases:
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(sys, "argv", ["bridge", command, "--settings", "/fixture/settings.json", "--start"]), \
                    patch(adapter, return_value=0) as install:
                self.assertEqual(main(), 0)
                self.assertTrue(install.call_args.args[0].start)
