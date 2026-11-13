"""Actual registered plugin + worker + stdio MCP in a fresh home, without a provider call."""
from __future__ import annotations

import argparse
import json
import os
import shlex
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
        "memory": {"memory_char_limit": 1200},
        "auxiliary": {"background_review": {"enabled": False}},
        "auth": {"adopt_external_logins": False}, "gateway": {"standalone": True},
        "plugins": {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}}}},
    })
    plugin = home / "plugins/codex-learning-bridge"
    plugin.parent.mkdir()
    plugin.symlink_to(ROOT / "hermes-plugin", target_is_directory=True)
    skill = home / "skills/update-contract/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: update-contract\ndescription: Isolated update contract fixture.\n"
                     "required_commands: [missing-hermes-learning-fixture-command]\n"
                     "---\nCheck the candidate receipt.\n")
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
        if values[0]["memory_capacity"]["files"]["MEMORY.md"]["limit_chars"] != 1200:
            raise RuntimeError("Candidate did not read the configured native memory capacity")
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
        recall = call_mcp([calls[1]])[0]["result"]["structuredContent"]
        entry = next(item for item in recall["skills"] if item["name"] == "update-contract")
        if entry.get("artifact_version") != skill_reply["version"]:
            raise RuntimeError("Recalled artifact version did not match the delivered body")
        if skill_reply["host_readiness"].get("missing_commands") != ["missing-hermes-learning-fixture-command"]:
            raise RuntimeError("Skill loading did not report its declared host requirement")
        feedback_call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "feedback_status", "arguments": {"cwd": str(project), "chat": "fixture-chat"}}}
        feedback = call_mcp([feedback_call])[0]["result"]["structuredContent"]
        if feedback["state"] != "awaiting_capture" or feedback["test_references"]:
            raise RuntimeError("An empty chat must not inherit feedback from another chat")
        checks = {
            "memory-preview": ["--cwd", str(project)],
            "publication-preview": ["--cwd", str(project)],
            "skill-readiness": ["--cwd", str(project), "--name", "update-contract", "--artifact-version", skill_reply["version"]],
            "review-metrics": ["--cwd", str(project), "--date", "2026-01-01"],
        }
        cli = {command: json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/bridge"), command,
                    "--settings", str(config), *flags], text=True, capture_output=True, check=True, timeout=15).stdout)
               for command, flags in checks.items()}
        if cli["skill-readiness"]["scope"] != "execution_host" or cli["skill-readiness"]["missing_commands"] != [
                "missing-hermes-learning-fixture-command"]:
            raise RuntimeError("Execution host did not check the exact loaded skill's prerequisites")
        if cli["memory-preview"]["native_limits"]["MEMORY.md"]["limit_chars"] != 1200:
            raise RuntimeError("Memory preview did not preserve the configured native capacity")
        if cli["publication-preview"]["visible_if_checked"] != 0 or cli["review-metrics"]["groups"]["other"]["attempts"]:
            raise RuntimeError("Read-only diagnostics reported evidence absent from this isolated home")
        from hermes_codex_learning.capture import hook
        study_scope = {"cwd": str(project), "session_id": "fixture-study", "turn_id": "fixture-study-turn"}
        # Exercise a distinct source chat before reuse. The skill itself is a
        # pre-seeded artifact and this smoke deliberately makes no model call.
        for event in (
            {"hook_event_name": "UserPromptSubmit", "prompt": "Review the fixture approach"},
            {"hook_event_name": "PostToolUse", "tool_use_id": "fixture-source-load",
             "tool_name": "mcp__hermes_learning__skill_load", "tool_response": {"structuredContent": skill_reply}},
            {"hook_event_name": "Stop", "last_assistant_message": "The fixture approach is documented"},
        ):
            hook({**study_scope, **event}, settings)
        skill_call["params"]["arguments"]["artifact_version"] = entry["artifact_version"]
        skill_reply = call_mcp([skill_call])[0]["result"]["structuredContent"]
        scope = {"cwd": str(project), "session_id": "fixture-chat", "turn_id": "fixture-turn"}
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", str(project)]
        (project / "test_update_contract.py").write_text(
            "import unittest\nclass Contract(unittest.TestCase):\n    def test_fixture(self):\n        self.assertEqual(1, 1)\n")
        test = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=15)
        if test.returncode != 0 or "Ran 1 test" not in test.stderr:
            raise RuntimeError("Isolated execution did not run the fixture test")
        for event in (
            {"hook_event_name": "UserPromptSubmit", "prompt": "Verify the recalled fixture method"},
            {"hook_event_name": "PostToolUse", "tool_use_id": "fixture-recall",
             "tool_name": "mcp__hermes_learning__context", "tool_response": {"structuredContent": recall}},
            {"hook_event_name": "PostToolUse", "tool_use_id": "fixture-load",
             "tool_name": "mcp__hermes_learning__skill_load", "tool_response": {"structuredContent": skill_reply}},
            {"hook_event_name": "PostToolUse", "tool_use_id": "fixture-test", "tool_name": "Bash",
             "tool_input": {"command": shlex.join(command)}, "tool_response": {"exit_code": test.returncode}},
            {"hook_event_name": "Stop", "last_assistant_message": "The fixture test passed"},
        ):
            hook({**scope, **event}, settings)
        feedback_params = {"cwd": scope["cwd"], "chat": scope["session_id"], "turn": scope["turn_id"]}
        deadline = time.monotonic() + 10
        while True:
            feedback = request(settings, "feedback_status", feedback_params)
            refs = feedback["test_references"]
            if refs and refs[0]["outcome"] == "completed":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("The isolated worker did not consume the actual test exit code")
            time.sleep(0.05)
        if feedback["deliveries"] != [{"name": skill_reply["name"], "version": skill_reply["version"]}]:
            raise RuntimeError("The worker did not persist the recalled skill version")
        submission = {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "record_application", "arguments": {"cwd": str(project), "stage": "verified",
            "delivery_token": skill_reply["delivery_token"], "source_events": [refs[0]["source_event"]]}}}
        app_receipt = call_mcp([submission])[0]["result"]["structuredContent"]
        readback = request(settings, "feedback_status", {**feedback_params, "receipt_id": app_receipt["id"]})["receipt"]
        if (readback["validation"] != "host_test_passed" or readback["version"] != skill_reply["version"]
                or readback["test_outcomes"] != refs):
            raise RuntimeError("Copyable test feedback did not settle for the loaded skill version")
        calls.extend([skill_call, feedback_call, submission])
    finally:
        worker.stop()
        temporary.cleanup()
    if worker.process.returncode != 0:
        raise RuntimeError(f"Worker failed clean shutdown: {worker.process.returncode}")
    receipt = {"status": "passed", "registered_plugin": True, "startup_paused": True,
               "resumed": True, "build": values[0]["build"],
               "dependency_mode": values[0].get("dependency_mode"),
               "clean_shutdown": True, "mcp": [call["params"]["name"] for call in calls], "cli": sorted(cli),
               "feedback_validation": readback["validation"], "test_outcome_source": refs[0]["outcome_source"],
               "fixture_cross_chat": True, "preseeded_artifact": True, "artifact_version": readback["version"],
               "real_provider": False, "actual_App": False}
    atomic_json(output_workspace / "result.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
