"""Actual registered plugin + worker + stdio MCP in a fresh home, without a provider call."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_codex_learning.config import Settings
from hermes_codex_learning.ipc import request
from hermes_codex_learning.store import atomic_json
from hermes_codex_learning.supervisor import Worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--fallback-python", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, mode=0o700)  # Never overwrite an existing fixture.
    output_workspace = workspace
    # macOS AF_UNIX paths are limited to 104 bytes. Candidate trees can be much
    # deeper; only the ephemeral probe home/socket needs a short local path.
    temporary = tempfile.TemporaryDirectory(prefix="hl-", dir="/tmp")
    workspace = Path(temporary.name).resolve()
    home, project = workspace / "home", workspace / "project"
    home.mkdir()
    project.mkdir()
    sys.path.insert(0, str(args.source.resolve()))
    os.environ["HERMES_HOME"] = str(home)
    if sys.platform == "darwin" and sys.version_info[:2] == (3, 14):
        sys.modules["_ruamel_yaml"] = None
    from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
    atomic_config_write(home / "config.yaml", {
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "model": {"provider": "custom", "default": "fixture", "base_url": "http://127.0.0.1:1/v1"},
        "auth": {"adopt_external_logins": False}, "gateway": {"standalone": True},
        "plugins": {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}}}},
    })
    plugin = home / "plugins/codex-learning-bridge"
    plugin.parent.mkdir()
    plugin.symlink_to(ROOT / "hermes-plugin", target_is_directory=True)
    skill = home / "skills/update-contract/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: update-contract\ndescription: Isolated update contract fixture.\n---\nCheck the candidate receipt.\n")
    config = home / "bridge.json"
    atomic_json(config, {"home": str(home), "project": str(project), "source": str(args.source.resolve()),
                         "python": sys.executable, "notifications": False, "capture_scope": "global",
                         "fallback_python": str(args.fallback_python) if args.fallback_python else None,
                         "updates": {"mode": "off"}})
    worker = Worker(ROOT)
    try:
        settings = Settings.load(config)
        worker.start(settings, config, paused=True)
        worker.ready()
        calls = [{"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": params}}
                 for index, (name, params) in enumerate([
                     ("learning_status", {}), ("context", {"cwd": str(project)}), ("update_status", {})], 1)]
        def call_mcp(messages):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/bridge"), "mcp"],
                                    env={**os.environ, "HERMES_LEARNING_CONFIG": str(config)},
                                    input="".join(json.dumps(call) + "\n" for call in messages),
                                    text=True, capture_output=True, check=True, timeout=60)
            return [json.loads(line) for line in result.stdout.splitlines()]
        replies = call_mcp(calls)
        if len(replies) != len(calls) or any("error" in reply for reply in replies):
            raise RuntimeError("Installed MCP contract failed")
        values = [reply["result"]["structuredContent"] for reply in replies]
        if values[0]["state"] != "update-paused" or values[0]["snapshot"]["attempts"]:
            raise RuntimeError("Candidate began learning before activation")
        if args.fallback_python and values[0].get("dependency_mode") != "official_shared":
            raise RuntimeError("Candidate did not lease the official dependency environment")
        skill_call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "skill_load", "arguments": {"cwd": str(project), "name": "update-contract"}}}
        if "error" not in call_mcp([skill_call])[0]:
            raise RuntimeError("Candidate issued a durable skill receipt before activation")
        worker.resume()
        deadline = time.monotonic() + 5
        while request(settings, "learning_status")["state"] != "idle":
            if time.monotonic() >= deadline:
                raise RuntimeError("Activated worker did not leave the paused state")
            time.sleep(0.1)
        skill_reply = call_mcp([skill_call])[0]["result"]["structuredContent"]
        if "Check the candidate receipt." not in skill_reply["content"] or not skill_reply["content_hash"]:
            raise RuntimeError("Native skill body/version delivery failed")
        calls.append(skill_call)
    finally:
        worker.stop()
        temporary.cleanup()
    if worker.process.returncode != 0:
        raise RuntimeError(f"Worker failed clean shutdown: {worker.process.returncode}")
    receipt = {"status": "passed", "registered_plugin": True, "startup_paused": True,
               "resumed": True,
               "dependency_mode": values[0].get("dependency_mode"),
               "clean_shutdown": True, "mcp": [call["params"]["name"] for call in calls],
               "real_provider": False, "actual_App": False}
    atomic_json(output_workspace / "result.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
