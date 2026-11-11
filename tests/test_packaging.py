import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning import __version__
from hermes_codex_learning.cli import install_notifier


class PackagingTests(unittest.TestCase):
    def test_notifier_replacement_waits_for_running_app_to_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Hermes 学习.app"
            info = app / "Contents/Info.plist"
            info.parent.mkdir(parents=True)
            installed_plist = plistlib.dumps({"CFBundleIdentifier": "com.cy.hermes-learning-receipts"})
            info.write_bytes(installed_plist)

            def refuse_to_exit(command, **kwargs):
                if command[1:2] == ["--prepare-upgrade"]:
                    self.assertEqual(command[2], str(app))
                    self.assertEqual(info.read_bytes(), installed_plist)
                    raise subprocess.CalledProcessError(1, command)

            with patch("hermes_codex_learning.notify.notifier_app", return_value=app), \
                    patch("hermes_codex_learning.cli.subprocess.run", side_effect=refuse_to_exit):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_notifier()

            self.assertEqual(info.read_bytes(), installed_plist)

    def test_notifier_migration_keeps_legacy_sender_until_new_sender_is_authorized(self):
        legacy_id = "com.cy.hermes-learning-notifier"
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Hermes 学习.app"
            info = app / "Contents/Info.plist"
            info.parent.mkdir(parents=True)
            legacy_plist = plistlib.dumps({"CFBundleIdentifier": legacy_id})
            info.write_bytes(legacy_plist)

            def deny_authorization(command, **kwargs):
                if command[1:] == ["--authorize"]:
                    self.assertEqual(info.read_bytes(), legacy_plist)
                    raise subprocess.CalledProcessError(1, command)

            with patch("hermes_codex_learning.notify.notifier_app", return_value=app), \
                    patch("hermes_codex_learning.cli.subprocess.run", side_effect=deny_authorization):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_notifier()

            self.assertEqual(info.read_bytes(), legacy_plist)

    def test_manifest_starts_mcp_without_hook_environment_or_placeholder_expansion(self):
        root = Path(__file__).resolve().parents[1]
        server = json.loads((root / ".mcp.json").read_text())["mcpServers"]["hermes_learning"]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.json"
            config.write_text(json.dumps({
                "home": directory, "project": directory, "source": directory,
                "python": sys.executable,
            }))
            env = {key: value for key, value in os.environ.items()
                   if key not in {"PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"}}
            env["HERMES_LEARNING_CONFIG"] = str(config)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            result = subprocess.run([server["command"], *server.get("args", [])],
                                    cwd=root / server.get("cwd", "."), env=env,
                                    input="".join(json.dumps(message) + "\n" for message in messages),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(replies[0]["result"]["serverInfo"]["version"], __version__)
            self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]},
                             {"learning_status", "context", "skill_load", "update_status", "update_check"})


if __name__ == "__main__":
    unittest.main()
