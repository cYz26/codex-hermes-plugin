import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook
from hermes_codex_learning.claude_hooks import EXECUTOR, PLUGIN_MCP_PREFIX, POST_TOOL_SUCCESS, SHELL_FAILURE_EXIT
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.knowledge import reconcile
from hermes_codex_learning.read_receipts import issue
from hermes_codex_learning.store import atomic_json, digest


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.other = self.root / "other"
        self.project.mkdir()
        self.other.mkdir()
        self.settings = Settings(self.root / "learning", self.project, self.root, Path(sys.executable),
                                 capture_scope="global", notifications=False)
        self.observed = []
        self.usage = []
        api = SimpleNamespace(open=lambda source: {"handle": source["chat"]}, observe=self.observe,
                              record_usage=lambda *args: self.usage.append(args),
                              read=lambda **kwargs: {"delivery_token": "native-token", "version": "v1"})
        self.bridge = Bridge(self.settings, api)
        self.store = self.bridge.store
        self.addCleanup(self.store.close)
        self.enterContext(patch("hermes_codex_learning.host_evidence.supplement",
                                side_effect=AssertionError("Claude must use its own hook evidence")))

    def observe(self, handle, body, *, event_id):
        self.observed.append((event_id, body))
        return {"revision": len(self.observed)}

    def send(self, event, turn="prompt-1", chat="claude-session", cwd=None, **fields):
        return hook({"hook_event_name": event, "cwd": str(cwd or self.project), "session_id": chat,
                     "prompt_id": turn, "transcript_path": "/unused/private.jsonl", **fields},
                    self.settings, host="claude")

    def bash(self, event="PostToolUse", call="test", turn="prompt-1", **fields):
        return self.send(event, turn, tool_use_id=call, tool_name="Bash",
                         tool_input=fields.pop("tool_input", {"command": "python3 -B -m unittest"}), **fields)

    def load(self, token, **fields):
        result = {"name": "fixture", "version": "v1", "delivery_token": token, "content": "Fixture skill body"}
        self.send("PostToolUse", tool_name=PLUGIN_MCP_PREFIX + "skill_load", tool_use_id="load",
                  tool_response=json.dumps(result), **fields)

    def saved(self, turn="prompt-1"):
        row = self.store.db.execute("SELECT payload FROM turns WHERE id=?", (digest(["claude-session", turn]),)).fetchone()
        return json.loads(row["payload"])

    def test_round_trip_keeps_test_provenance_and_uses_shared_explicit_learning(self):
        self.send("UserPromptSubmit", prompt="Run the tests after loading the fixture skill")
        token = issue(self.settings.state,
                      {"name": "fixture", "version": "v1", "file_path": "SKILL.md", "content_hash": "sha"},
                      str(self.project))
        self.load(token)
        cases = [
            ("pass", "PostToolUse", {"tool_response": {"stdout": "OK", "stderr": "", "interrupted": False}}),
            ("fail", "PostToolUseFailure", {"error": "Exit code 2\nfailed"}),
            ("abort", "PostToolUseFailure", {"error": "Exit code 130", "is_interrupt": True}),
            ("blocked", "PostToolUseFailure", {"error": "Permission denied"}),
            ("background", "PostToolUse", {"tool_response": {"stdout": "", "stderr": "", "interrupted": False,
                                                              "backgroundTaskId": "fixture-task"}}),
        ]
        for call, event, fields in cases:
            self.bash("PreToolUse", call)
            self.bash(event, call, **fields)
        self.bash("PreToolUse", "no-result")
        self.send("Stop", last_assistant_message="Verification finished", background_tasks=[{"id": "fixture-task"}])
        self.send("Stop", last_assistant_message="Verification finished", background_tasks=[{"id": "fixture-task"}])
        self.bridge.ingest()
        self.assertEqual(len(self.observed), 1)
        body = self.observed[0][1]
        self.assertEqual((body["source"]["executor"], body["source"]["turn"], body["status"]),
                         (EXECUTOR, "prompt-1", "completed"))
        self.assertIn("claude_tool_result_unavailable", body["coverage"])
        self.assertIn("claude_session_background_tasks", body["coverage"])
        self.assertNotIn("private.jsonl", json.dumps([row[0] for row in self.store.db.execute("SELECT payload FROM events")]))
        tools = self.saved()["tools"]
        self.assertNotIn("exit_code", tools["pass"]["result"])
        self.assertEqual(tools["fail"]["result"]["exit_code"], 2)
        self.assertNotIn("exit_code", tools["abort"]["result"])
        scope = {"cwd": str(self.project), "chat": "claude-session", "turn": "prompt-1"}
        feedback = self.bridge.rpc("feedback_status", scope)
        refs = {item["source_event"]["tool_call"]: item for item in feedback["test_references"]}
        self.assertEqual(refs["pass"]["outcome_source"], POST_TOOL_SUCCESS)
        self.assertEqual(refs["fail"]["outcome_source"], SHELL_FAILURE_EXIT)
        self.assertEqual(refs["abort"]["outcome"], "interrupted")
        self.assertEqual([refs[call]["outcome"] for call in ("blocked", "background", "no-result")], ["unknown"] * 3)
        self.assertEqual(len(self.usage), 1)
        app = self.bridge.rpc("record_application", {"cwd": str(self.project), "delivery_token": token,
                              "stage": "verified", "source_events": [refs["pass"]["source_event"]]})
        self.assertEqual(app["validation"], "host_test_passed")
        self.assertEqual(app["delivery_source_event"], ["claude-session", "prompt-1", "load"])
        request = self.bridge.rpc("request_learning", {"cwd": str(self.project), "chat_id": "claude-session",
                                  "request_id": "learn-fixture", "focus": "Retain the check"})
        self.assertEqual(request["state"], "queued")
        self.assertEqual(request["evidence"]["turns"], ["prompt-1"])
        with self.assertRaises(ValueError):
            self.bridge.rpc("request_learning", {"cwd": str(self.other), "chat_id": "claude-session",
                            "request_id": "foreign-fixture", "focus": "Other project"})

    def test_late_results_and_stop_continuation_only_add_new_observations(self):
        self.send("UserPromptSubmit", prompt="Check a fixture")
        self.bash("PreToolUse")
        self.send("Stop", last_assistant_message="Check complete")
        self.bridge.ingest()
        self.assertEqual(len(self.observed), 1)
        response = {"stdout": "OK", "stderr": "", "interrupted": False}
        self.bash(tool_response=response)
        self.bridge.ingest()
        self.assertEqual(len(self.observed), 2)
        self.assertEqual(self.saved()["coverage"], [])
        for call in ("continued", "continued-again"):
            self.bash("PreToolUse", call)
            self.bash(call=call, tool_response=response)
            self.bridge.ingest()
            self.assertEqual(self.saved()["status"], "running")
            before = len(self.observed)
            self.send("Stop", stop_hook_active=True, last_assistant_message="Check complete")
            self.send("Stop", stop_hook_active=True, last_assistant_message="Check complete")
            self.bridge.ingest()
            self.assertEqual(self.saved()["status"], "completed")
            self.assertEqual(len(self.observed), before + 1)
        self.assertEqual(len({event for event, _ in self.observed}), 4)

    def test_missing_stop_and_api_failure_remain_incomplete(self):
        self.send("UserPromptSubmit", prompt="Interrupted first prompt")
        self.bash("PreToolUse")
        self.send("UserPromptSubmit", "prompt-2", prompt="Fails at the API")
        self.send("StopFailure", "prompt-2", last_assistant_message="Private API error")
        self.send("StopFailure", "prompt-2", last_assistant_message="Private API error")
        self.bridge.ingest()
        self.assertEqual(self.saved()["_ended_by"], "superseded_by_prompt")
        self.assertEqual(self.saved("prompt-2")["_ended_by"], "StopFailure")
        self.assertFalse(self.bridge.has_active_turns())
        self.assertEqual(self.saved("prompt-2")["assistant"], "")
        self.bash(tool_response={"stdout": "late", "stderr": "", "interrupted": False})
        self.send("UserPromptSubmit", "prompt-3", prompt="Ends with the session")
        self.send("SessionEnd", turn=None)
        self.bridge.ingest()
        self.assertFalse(self.observed)
        self.assertEqual(self.saved()["status"], "incomplete")
        self.assertEqual(self.saved("prompt-3")["_ended_by"], "SessionEnd")
        self.send("UserPromptSubmit", "prompt-4", prompt="Ends by timeout")
        self.bridge.ingest()
        with patch("time.time", return_value=time.time() + self.settings.active_turn_timeout_seconds + 1):
            self.assertEqual(self.bridge.reap_stale_turns(), 1)
        self.assertEqual(self.saved("prompt-4")["_ended_by"], "stale_timeout")

    def test_workflow_projection_preserves_outcome_without_capturing_body(self):
        policy = {"enabled": True, "workflow_projection": True}
        self.settings = self.bridge.settings = replace(self.settings, data_policy=policy)
        self.send("UserPromptSubmit", prompt="private fixture subject")
        self.bash(tool_response={"stdout": "private fixture output", "stderr": "", "interrupted": False})
        self.bash("PostToolUseFailure", "blocked", error="Permission denied")
        self.send("Stop", last_assistant_message="private fixture answer")
        self.bridge.ingest()
        self.assertNotIn("private fixture", json.dumps(self.observed))
        self.assertIn('"outcome":"completed"', self.observed[0][1]["user"])
        refs = self.bridge.rpc("feedback_status", {"cwd": str(self.project), "chat": "claude-session"})["test_references"]
        self.assertEqual({ref["source_event"]["tool_call"]: ref["outcome"] for ref in refs},
                         {"test": "completed", "blocked": "unknown"})

    def test_recall_uses_project_scope_for_both_hosts(self):
        context = {"version": "fixture", "memories": {"MEMORY.md": "Fixture project convention"}, "skills": []}
        context["bridge_provenance"] = reconcile({}, context, bootstrap_project=str(self.project))
        atomic_json(self.settings.state / "context.json", context)
        claude = self.send("SessionStart", turn=None)
        codex = hook({"hook_event_name": "SessionStart", "session_id": "codex-session", "cwd": str(self.project)}, self.settings)
        self.assertEqual(claude, codex)
        text = claude["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Fixture project convention", text)
        self.assertLessEqual(len(text.encode()), self.settings.session_context_limit)
        self.assertEqual(self.send("SessionStart", turn=None, cwd=self.other, chat="other-session"), {})
        self.assertEqual(self.send("SessionStart", turn=None), claude)  # Resume or compaction.
        self.send("UserPromptSubmit", turn=None, prompt="unbound body")
        self.send("Stop", turn=None, last_assistant_message="unbound result")
        self.send("UserPromptSubmit", "subagent", prompt="subagent body", agent_id="agent-1")
        self.bridge.ingest()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        self.assertNotIn("unbound", json.dumps([row[0] for row in self.store.db.execute("SELECT payload FROM events")]))
