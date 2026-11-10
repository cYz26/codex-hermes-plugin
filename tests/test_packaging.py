import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning import __version__


class PackagingTests(unittest.TestCase):
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
                             {"learning_status", "context", "skill_load", "update_status", "update_check", "record_application"})


if __name__ == "__main__":
    unittest.main()
