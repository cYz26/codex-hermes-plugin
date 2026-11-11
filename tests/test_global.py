import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.delegation import register_delegation, task_input
from hermes_codex_learning.knowledge import ENTRY_DELIMITER, context_view, reconcile
from hermes_codex_learning.store import Store, atomic_json


class GlobalTests(unittest.TestCase):
    def setUp(self):
        # The host may itself keep /tmp or its home in Git. This case needs a
        # genuinely non-Git fixture, not a work directory inside that repository.
        parent = Path("/var/tmp")
        neutral = parent.is_dir() and not any((item / ".git").exists() for item in (parent, *parent.parents))
        self.temp = tempfile.TemporaryDirectory(dir=str(parent) if neutral else None)
        self.root = Path(self.temp.name).resolve()
        self.a, self.b = self.root / "pilot", self.root / "other project"
        self.a.mkdir()
        self.b.mkdir()
        (self.b / ".git").mkdir()
        (self.b / "src").mkdir()
        self.settings = Settings(self.root / "home", self.a, self.root, Path("/usr/bin/python3"), capture_scope="global")
        self.store = Store(self.settings.state)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, cwd, chat, event="UserPromptSubmit", **kwargs):
        return {"cwd": str(cwd), "session_id": chat, "turn_id": "turn-" + chat,
                "hook_event_name": event, "prompt": "Validate regression tests", **kwargs}

    def test_global_capture_keeps_two_project_identities_and_deduplicates(self):
        event = self.event(self.b / "src", "b")
        hook(self.event(self.a, "a"), self.settings)
        hook(event, self.settings)
        hook(event, self.settings)
        payloads = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM events")]
        self.assertEqual(len(payloads), 2)
        self.assertEqual({p["bridge_project"] for p in payloads}, {str(self.a), str(self.b)})
        self.assertEqual(len({p["bridge_host"] for p in payloads}), 1)
        self.assertEqual(hook(self.event(self.b, "sub", agent_id="agent-1"), self.settings), {})

    def test_project_mode_remains_opt_in_and_exclusions_precede_global_capture(self):
        self.assertIsNone(replace(self.settings, capture_scope="project").project_for(str(self.b)))
        blocked = replace(self.settings, excluded_projects=(str(self.b),))
        self.assertEqual(hook(self.event(self.b / "src", "excluded"), blocked), {})
        for cwd in (None, "", "relative", "ssh://remote/home"):
            self.assertIsNone(self.settings.project_for(cwd))
        with self.assertRaises(ValueError):
            replace(self.settings, capture_scope="typo")

    def test_worktrees_non_git_and_symlinks_have_stable_real_paths(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /not-opened")
        folder = worktree / "src"
        folder.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(folder)
        self.assertEqual(self.settings.project_for(str(alias)), str(worktree))
        self.assertEqual(self.settings.project_for(str(self.root)), str(self.root))
        self.assertEqual(self.settings.project_for("/"), "/")

    def test_global_hooks_keep_learning_notices_out_of_all_chats(self):
        self.store.notice({"id": "b-result", "message": "B result", "project": str(self.b)})
        self.store.notice({"id": "b-load", "message": "B loaded", "project": str(self.b), "chat": "b"})
        self.assertNotIn("systemMessage", hook(self.event(self.a, "a"), self.settings))
        self.assertNotIn("systemMessage", hook(self.event(self.b, "b-other"), self.settings))
        self.assertNotIn("systemMessage", hook(self.event(self.b, "b"), self.settings))
        self.assertEqual(len(self.store.consume("os-notification")), 2)

    def context(self):
        context = {"memories": {"MEMORY.md": "Pilot-specific convention", "USER.md": ""},
                   "skills": [{"name": "regression-tests", "description": "Validate regression tests", "version": "v1"},
                              {"name": "pilot-deploy", "description": "Pilot provisioning convention", "version": "v1"}],
                   "provider_recall": "Unscoped provider recall from another chat"}
        applied = {"applied": [{"tool": "skill_manage", "name": name} for name in ("regression-tests", "pilot-deploy")]}
        index = reconcile({}, context, bootstrap_project=str(self.a))
        index = reconcile(index, context, project=str(self.a), result=applied)
        return {**context, "bridge_provenance": index}

    def test_shared_skills_recalled_by_relevance_without_foreign_memory(self):
        context = self.context()
        own = context_view(context, str(self.a))
        other = context_view(context, str(self.b), "Validate regression tests")
        self.assertIn("Pilot-specific", own["summary"])
        self.assertFalse(other["memories"])
        self.assertEqual([s["name"] for s in other["skills"]], ["regression-tests"])
        self.assertEqual(other["skills"][0]["source_projects"], [str(self.a)])
        self.assertFalse(other["provider_recall"])
        self.assertTrue(other["unscoped_provider_recall_withheld"])
        self.assertFalse(context_view(context, None)["memories"])

    def test_native_memory_changes_are_scoped_and_unattributed_changes_withheld(self):
        context = self.context()
        updated = {**context, "memories": {"MEMORY.md": "Pilot-specific convention" + ENTRY_DELIMITER + "Other-project convention"}}
        index = reconcile(context["bridge_provenance"], updated, project=str(self.b), result={"applied": [{"tool": "memory"}]})
        result = context_view({**updated, "bridge_provenance": index}, str(self.b))
        self.assertEqual(result["memories"]["MEMORY.md"], "Other-project convention")
        unknown = {"memories": {"MEMORY.md": "Changed outside a known review"}}
        index = reconcile(index, unknown)
        self.assertFalse(context_view({**unknown, "bridge_provenance": index}, str(self.b))["memories"])
        self.assertFalse(context_view({**unknown, "bridge_provenance": index}, str(self.a))["memories"])

    def test_hooks_publish_global_availability_without_dumping_foreign_memory(self):
        atomic_json(self.settings.state / "context.json", self.context())
        result = hook(self.event(self.b, "b"), self.settings)
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("全局自动学习", text)
        self.assertIn(str(self.b), text)
        self.assertIn("regression-tests", text)
        self.assertNotIn("Pilot-specific", text)
        self.assertNotIn("hookSpecificOutput", hook(self.event(self.b, "b"), self.settings))
        self.assertIn("hookSpecificOutput", hook(self.event(self.b, "new-chat"), self.settings))

    def test_hook_bounds_all_injected_utf8_bytes_for_long_multilingual_memory(self):
        context = self.context()
        context["memories"]["MEMORY.md"] += ENTRY_DELIMITER + "经验😀e\u0301「x=0x9f」" * 120
        context["bridge_provenance"] = reconcile(context["bridge_provenance"], context, project=str(self.b),
                                                 result={"applied": [{"tool": "memory"}]})
        atomic_json(self.settings.state / "context.json", context)
        output = hook(self.event(self.b, "b"), self.settings)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(output.encode("utf-8")), 256)
        self.assertIn(str(self.b), output)
        self.assertIn("hermes_learning.context", output)
        self.assertNotIn("\ufffd", output)

    def test_overlong_project_path_keeps_recall_hint_without_partial_path(self):
        long_project = self.root / ("长" * 65) / ("路径" * 35)
        long_project.mkdir(parents=True)
        result = hook(self.event(long_project, "long-path"), self.settings)
        output = result["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(output.encode("utf-8")), 256)
        self.assertIn("hermes_learning.context", output)
        self.assertNotIn("长" * 8, output)

    def test_foreign_tool_workdir_cannot_switch_an_existing_chat_scope(self):
        context = self.context()
        context["memories"]["MEMORY.md"] += ENTRY_DELIMITER + "Only in project B"
        context["bridge_provenance"] = reconcile(context["bridge_provenance"], context, project=str(self.b),
                                                 result={"applied": [{"tool": "memory"}]})
        atomic_json(self.settings.state / "context.json", context)
        self.store.notice({"id": "b-review", "message": "Only for project B", "project": str(self.b)})
        # Start with 0.2.0 data: the genuine prompt exists, with no cached chat workspace.
        self.store.capture({**self.event(self.a, "a"), "bridge_project": str(self.a)})
        result = hook(self.event(self.b / "src", "a", "PostToolUse", tool_use_id="foreign-cwd"), self.settings)
        self.assertNotIn("systemMessage", result)
        recall = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Pilot-specific convention", recall)
        self.assertNotIn("Only in project B", recall)
        payload = json.loads(self.store.db.execute("SELECT payload FROM events ORDER BY created DESC LIMIT 1").fetchone()[0])
        self.assertEqual(payload["bridge_project"], str(self.a))
        self.assertEqual(payload["cwd"], str(self.b / "src"))

    def test_unbound_and_excluded_chats_cannot_recall_by_command_workdir(self):
        atomic_json(self.settings.state / "context.json", self.context())
        tool = self.event(self.a, "unbound", "PostToolUse", tool_use_id="foreign-cwd")
        self.assertEqual(hook(tool, self.settings), {})
        blocked = replace(self.settings, excluded_projects=(str(self.b),))
        hook(self.event(self.b, "excluded"), blocked)
        self.assertEqual(hook({**tool, "session_id": "excluded"}, blocked), {})
        self.assertFalse(self.store.pending())

    def test_mcp_uses_explicit_cwd_and_shared_skill_reads_keep_native_receipts(self):
        import hashlib
        artifact=self.root/"artifact"
        artifact.mkdir()
        (artifact/"SKILL.md").write_text("procedure")
        class API:
            def read(self, **kwargs):
                return {"name": kwargs["name"], "version": "immutable-version", "delivery_token": "fixture", "content": "procedure",
                        "resource_dir":str(artifact),"files":{"SKILL.md":hashlib.sha256(b"procedure").hexdigest()},
                        "file_path":"SKILL.md","content_hash":hashlib.sha256(b"procedure").hexdigest()}
        atomic_json(self.settings.state / "context.json", self.context())
        self.store.set("knowledge-index-v1", self.context()["bridge_provenance"])
        bridge = Bridge(self.settings, API())
        try:
            bridge.cache_artifacts(self.context())
            self.assertFalse(bridge.rpc("context", {})["memories"])
            self.assertFalse(bridge.rpc("context", {"cwd": str(self.b)})["memories"])
            self.assertTrue(bridge.rpc("context", {"cwd": str(self.a)})["memories"])
            loaded = bridge.rpc("skill_load", {"name": "regression-tests", "cwd": str(self.b)})
            self.assertTrue(loaded["delivery_token"].startswith("read-v1."))
            self.assertEqual(loaded["source_projects"], [str(self.a)])
            self.assertEqual(loaded["request_project"], str(self.b))
            with self.assertRaises(ValueError):
                bridge.rpc("context", {"cwd": "relative"})
        finally:
            bridge.store.close()

    def test_cross_project_delegation_needs_actual_destination_hook(self):
        parent = {"hook_event_name": "PostToolUse", "session_id": "a", "turn_id": "a-turn", "tool_use_id": "create",
                  "tool_name": "mcp__codex_app__create_thread", "bridge_host": "host", "bridge_project": str(self.a),
                  "tool_input": {"prompt": "Work in the other project"},
                  "tool_response": {"structuredContent": {"threadId": "b", "hostId": "local"}}}
        register_delegation(self.store, "parent-receipt", parent, settings=self.settings)
        data = {"source": {"host": "host", "project": str(self.b), "chat": "b", "turn": "turn-b"}, "user": ""}
        self.assertFalse(task_input(self.store, data, settings=self.settings)["user"])
        self.store.capture({**self.event(self.b, "b", "Stop"), "bridge_host": "host", "bridge_project": str(self.b)})
        result = task_input(self.store, data, settings=self.settings)
        self.assertIn("Work in the other project", result["user"])
        self.assertEqual(result["source"]["project"], str(self.b))

    def test_cross_project_public_readback_binds_child_project_and_exact_sender(self):
        parent = {"hook_event_name": "PostToolUse", "session_id": "a", "turn_id": "a-turn", "tool_use_id": "send",
                  "tool_name": "mcp__codex_app__send_message_to_thread", "bridge_host": "host", "bridge_project": str(self.a),
                  "tool_input": {"threadId": "b", "prompt": "Follow up"},
                  "tool_response": {"structuredContent": {"threadId": "b", "hostId": "local"}}}
        sender = self.store.capture(parent)
        read = {**parent, "tool_name": "mcp__codex_app__read_thread", "tool_use_id": "read", "tool_input": {"threadId": "b"},
                "tool_response": {"structuredContent": {"schemaVersion": 1,
                   "thread": {"id": "b", "hostId": "local", "cwd": str(self.b / "src")},
                   "turns": [{"id": "followup", "items": [{"type": "functionCallOutput", "namespace": "codex_app",
                     "name": "send_message_to_thread", "output": {"text": "<codex_delegation><source_thread_id>a</source_thread_id><input>Follow up</input></codex_delegation>"}}]}]}}}
        self.assertTrue(register_delegation(self.store, "read-receipt", read, settings=self.settings))
        result = task_input(self.store, {"source": {"host": "host", "project": str(self.b), "chat": "b", "turn": "followup"}, "user": ""}, settings=self.settings)
        self.assertIn("Follow up", result["user"])
        self.assertEqual(result["task_input_provenance"]["source_event"], sender)

    def test_cross_project_memory_merge_and_unproven_change_are_not_relabelled(self):
        context = self.context()
        changed = {"memories": {"MEMORY.md": "Merged project facts"}}
        for outcome in ({"applied": [{"tool": "memory"}]}, {"run_status": "unknown", "applied": []}):
            index = reconcile(context["bridge_provenance"], changed, project=str(self.b), result=outcome)
            for project in (str(self.a), str(self.b)):
                self.assertFalse(context_view({**changed, "bridge_provenance": index}, project)["memories"])

    def test_worker_preserves_native_api_review_and_migrates_only_peripheral_provenance(self):
        context = self.context()
        class API:
            def __init__(self):
                self.observed, self.reviewed = [], []
                self.context = {k: v for k, v in context.items() if k != "bridge_provenance"}
            def open(self, source):
                return {"handle": source["chat"]}
            def observe(self, handle, body, **kwargs):
                self.observed.append(body)
            def read(self, kind, name=None, file_path=None):
                if kind == "skill":
                    return {"version": "unchanged-fixture-skill"}
                if kind == "context":
                    return self.context
                return {"intents": [{"id": "b-intent", "handle": "b", "status": "queued"}],
                        "attempts": [{"applied": [{"tool": "skill_manage", "name": "regression-tests"}]}]}
            def run_review(self, handle, intent_id, *, attempt_id):
                self.reviewed.append((handle, intent_id, attempt_id))
                self.context = {**self.context, "memories": {"MEMORY.md": "Pilot-specific convention" + ENTRY_DELIMITER + "B convention"}}
                return {"run_status": "completed", "applied": [{"tool": "memory"}, {"tool": "skill_manage", "name": "regression-tests"}]}
        api = API()
        bridge = Bridge(self.settings, api)
        try:
            for cwd, chat in ((self.a, "a"), (self.b, "b")):
                hook(self.event(cwd, chat), self.settings)
                hook(self.event(cwd, chat, "Stop", last_assistant_message="Done"), self.settings)
            bridge.ingest()
            self.assertEqual({v["source"]["project"] for v in api.observed}, {str(self.a), str(self.b)})
            bridge.reviews()
            bridge.publish()
            self.assertEqual(api.reviewed[0][:2], ("b", "b-intent"))
            view = bridge.rpc("context", {"cwd": str(self.b)})
            self.assertEqual(view["memories"]["MEMORY.md"], "B convention")
            self.assertEqual(view["skills"][0]["source_projects"], sorted([str(self.a), str(self.b)]))
            notices = self.store.consume("test", project=str(self.b))
            self.assertEqual({v["kind"] for v in notices}, {"review.started", "review.finished"})
            receipt = next(v["receipt"] for v in notices if v["kind"] == "review.finished")
            self.assertIn("开始复盘", receipt)
            self.assertIn("复盘完成", receipt)
            self.assertFalse(self.store.consume("test-a", project=str(self.a)))
        finally:
            bridge.store.close()


if __name__ == "__main__":
    unittest.main()
