import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.normalize import merge_event
from hermes_codex_learning.shared_learning import capture_native, chat_id, enqueue, receipt
from hermes_codex_learning.store import Store, digest


class SharedTransportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "one"
        self.project.mkdir()
        self.settings = Settings(self.root / "owner", self.project, self.root, Path(sys.executable),
                                 capture_scope="global", checked_publication=True,
                                 shared_profile=self.root / "client", shared_client_id="a" * 48)
        self.store = Store(self.settings.state)
        self.addCleanup(self.store.close)
        self.identity = {"session": "conversation-1", "turn": "turn-1", "surface": "gui"}

    def capture(self, kind, **body):
        return capture_native(self.settings, self.store, str(self.project), {**self.identity, **body, "kind": kind})

    def test_hermes_contribution_preserves_executor_and_exact_background_exit(self):
        self.assertEqual(self.capture("finished", text="out of order")["state"], "awaiting_prompt")
        first = self.capture("prompt", text="Run the regression tests")
        self.assertEqual(self.capture("prompt", text="Run the regression tests"), first)
        self.capture("tool_start", tool="terminal", call="run", args={"command": "python3 -m unittest"})
        self.capture("tool_end", tool="terminal", call="run",
                     result={"session_id":"process-id","exit_code":None,"status":"yielded_to_background"})
        self.capture("finished", text="Waiting for the process")
        rows = self.store.pending()
        for row in rows:
            data = merge_event(self.store, row)
        self.assertEqual(data["source"]["executor"], "Hermes")
        self.assertEqual(data["tools"]["run"]["status"], "running")
        self.assertEqual(data["coverage"], ["background_exit_pending"])
        self.capture("tool_end", tool="process", call="poll", args={"action":"poll","session_id":"process-id"},
                     result={"session_id":"process-id","exit_code":1})
        data = merge_event(self.store, self.store.pending()[-1])
        self.assertEqual(list(data["tools"]), ["run"])
        self.assertEqual(data["tools"]["run"]["status"], "failed")
        self.assertEqual(data["source"]["chat"], chat_id(self.settings, "conversation-1"))

    def test_request_retry_is_idempotent_and_cross_project_receipt_is_empty(self):
        request = {"operation": "memory", "request_id": "stable-id", "surface": "cli",
                   "args": {"action": "add", "content": "A durable workspace fact."}}
        one = enqueue(self.store, str(self.project), request)
        self.assertEqual(one, enqueue(self.store, str(self.project), request))
        self.assertEqual(receipt(self.store, str(self.root / "two"), "stable-id")["state"], "unknown")
        with self.assertRaises(ValueError):
            enqueue(self.store, str(self.project), {**request, "args": {"action": "remove"}})

    def test_no_project_no_automatic_learning(self):
        value = capture_native(self.settings, self.store, None, {**self.identity, "kind": "prompt", "text": "Private text"})
        self.assertEqual(value["state"], "withheld")
        self.assertFalse(self.store.pending())

    def test_codex_completed_transcript_recovers_missing_hook_without_guessing_tool_exit(self):
        from hermes_codex_learning.host_evidence import recover_completions, supplement
        from hermes_codex_learning.normalize import CODEX_COMPLETION
        import time

        profile = self.settings.shared_profile
        profile.mkdir()
        (profile / "shared-learning-client.json").touch()
        codex = self.root / "codex"
        transcript = codex / "sessions" / "rollout.jsonl"
        transcript.parent.mkdir(parents=True)
        project = str(self.project)
        records = [
            {"type":"session_meta", "payload":{"id":"chat", "cwd":project}},
            {"type":"event_msg", "payload":{"type":"task_complete", "turn_id":"different-turn", "last_agent_message":"Unrelated"}},
            {"type":"event_msg", "payload":{"type":"item_completed", "turn_id":"run", "item":{
                "id":"test", "type":"CommandExecution", "status":"completed", "exit_code":0}}},
        ]
        def write_native_records():
            transcript.write_text("".join(json.dumps(item) + "\n" for item in records))
        write_native_records()
        base = {"cwd":project,"bridge_project":project,"bridge_host":"h","session_id":"chat","turn_id":"run",
                "transcript_path":str(transcript)}
        prompt = self.store.capture({**base,"hook_event_name":"UserPromptSubmit","prompt":"Run the project tests"})
        end = self.store.capture({**base,"hook_event_name":"PostToolUse","tool_name":"Bash","tool_use_id":"test",
                                  "tool_response":"output without an exit field","tool_input":{"command":"python3 -m unittest"}})
        for item in self.store.pending():
            merge_event(self.store,item)
        initial = time.time()
        with patch.dict(os.environ, {"CODEX_HOME":str(codex)}), patch("time.time", return_value=initial + 25):
            recover_completions(self.settings,self.store)
        self.assertEqual([item["id"] for item in self.store.pending()], [prompt,end])
        records.append({"type":"event_msg", "payload":{"type":"task_complete", "turn_id":"run", "last_agent_message":"Tests ran."}})
        write_native_records()
        with patch.dict(os.environ, {"CODEX_HOME":str(codex)}), patch("time.time", return_value=initial + 50):
            recover_completions(self.settings,self.store)
            raw = self.store.pending()[-1]
            data = merge_event(self.store,raw)
            enriched = supplement(self.store,data)
        self.assertEqual(enriched["_ended_by"],CODEX_COMPLETION)
        self.assertEqual(data["status"],"incomplete")
        self.assertEqual(enriched["status"],"completed")
        self.assertEqual(enriched["tools"]["test"]["result"]["exit_code"],0)

    def test_trusted_enterprise_shared_learning_enforces_boundary_and_scrubs_writes(self):
        from dataclasses import replace
        from hermes_codex_learning.enterprise import boundary
        from hermes_codex_learning.shared_learning import status
        from hermes_codex_learning.shared_storage import _memory_boundary_key, _visible_entries
        from test_enterprise_policy import enterprise_fixture

        policy, model_config = enterprise_fixture(self.root)
        enterprise_settings = replace(self.settings, data_policy=policy)
        approved = boundary(policy)
        self.assertIsNotNone(approved)

        with self.assertRaisesRegex(PermissionError, "restricted"):
            enqueue(self.store, str(self.project), {
                "operation": "memory", "request_id": "cookie-write", "surface": "cli",
                "args": {"action": "add", "content": "Cookie: session=secret-value"},
            }, settings=enterprise_settings)

        accepted = enqueue(self.store, str(self.project), {
            "operation": "memory", "request_id": "ok-write", "surface": "cli",
            "args": {"action": "add", "content": "Internal build uses make check."},
        }, settings=enterprise_settings)
        self.assertEqual(accepted["state"], "queued")

        self.store.set("publications-v1", {
            "learned-enterprise-ops": {
                "artifact": {"version": "v-ent"},
                "check": {"audience": "enterprise", "boundary_id": approved["id"]},
            }
        })
        visible_status = status(self.store, str(self.project), settings=enterprise_settings)
        self.assertEqual(visible_status["published"],
                         [{"name": "learned-enterprise-ops", "version": "v-ent", "audience": "enterprise"}])

        entry = "Internal build uses make check."
        self.store.set("shared-enterprise-memory-boundaries", {
            _memory_boundary_key(str(self.project), "memory", digest(entry)): approved["id"],
        })
        self.assertEqual(_visible_entries(enterprise_settings, self.store, str(self.project), "memory", [entry]), [entry])

        # Switching to non-enterprise mode hides enterprise-scoped shared memories without deleting them.
        public_settings = replace(enterprise_settings, data_policy={"enabled": True, "workflow_projection": True})
        self.assertEqual(_visible_entries(public_settings, self.store, str(self.project), "memory", [entry]), [])

        # Tampering with model_config invalidates the enterprise boundary and blocks new shared writes and reads.
        model_config.write_text("model: changed\n")
        self.assertEqual(_visible_entries(enterprise_settings, self.store, str(self.project), "memory", [entry]), [])
        self.assertEqual(status(self.store, str(self.project), settings=enterprise_settings)["published"], [])
        with self.assertRaisesRegex(PermissionError, "Enterprise learning boundary"):
            enqueue(self.store, str(self.project), {
                "operation": "memory", "request_id": "blocked-write", "surface": "cli",
                "args": {"action": "add", "content": "Another note."},
            }, settings=enterprise_settings)


@unittest.skipUnless(os.getenv("HERMES_LEARNING_NATIVE_SOURCE"), "requires the patched native test runtime")
class SharedNativeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ["HERMES_LEARNING_NATIVE_SOURCE"]
        sys.path.insert(0, source)
        try:
            from agent.shared_learning_scope import Scope
        except ImportError as exc:
            sys.path.remove(source)
            raise unittest.SkipTest("native runtime does not include the shared protocol") from exc
        cls.addClassCleanup(sys.path.remove, source)

    def setUp(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_codex_learning.daemon import Bridge
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.a = str(self.root / "one")
        self.b = str(self.root / "two")
        for path in (self.a, self.b):
            Path(path).mkdir()
        token = set_hermes_home_override(self.owner)
        self.addCleanup(reset_hermes_home_override, token)
        config = Settings(self.owner, Path(self.a), Path(os.environ["HERMES_LEARNING_NATIVE_SOURCE"]),
                          Path(sys.executable), capture_scope="global", checked_publication=True,
                          shared_profile=self.root / "client", shared_client_id="a" * 48)
        api = Mock()
        api.read.return_value = {"memories": {}, "skills": []}
        self.bridge = Bridge(config, api)
        self.addCleanup(self.bridge.store.close)
        self.bridge.ready.set()

    def view(self, project):
        return self.bridge.rpc("context", {"cwd": project} if project else {})

    def write(self, project, operation, args, base=None):
        from hermes_codex_learning.shared_storage import apply_write
        return apply_write(self.bridge, project, {"operation": operation, "args": args, "base": base})

    def test_independent_memory_store_user_approval_and_stale_write(self):
        a_initial = self.view(self.a)["memory_versions"]["memory"]
        b_initial = self.view(self.b)["memory_versions"]["memory"]
        self.assertEqual(a_initial, b_initial)
        applied, _ = self.write(self.a, "memory", {"action":"add", "content":"Project one uses fixture cobalt."}, a_initial)
        self.assertEqual(applied, "applied")
        self.assertIn("fixture cobalt", self.view(self.a)["memories"]["MEMORY.md"])
        self.assertNotIn("MEMORY.md", self.view(self.b)["memories"])
        self.assertEqual(self.write(self.a, "memory", {"action":"add", "content":"Stale change."}, a_initial)[0], "conflict")
        base = self.view(None)["memory_versions"]["user"]
        state, staged = self.write(self.a, "memory", {"target":"user","action":"add","content":"Prefer short progress updates."}, base)
        self.assertEqual(state, "pending_review")
        self.assertNotIn("USER.md", self.view(None)["memories"])
        from hermes_codex_learning.shared_storage import read
        item, = read(self.bridge, self.bridge.store, "pending", self.a, {"subsystem":"memory"})["records"]
        self.assertEqual(staged["pending_id"], item["id"])
        params = {"subsystem":"memory","decision":"approve","id":item["id"],"revision":item["revision"]}
        self.assertEqual(self.write(self.b, "decide", params)[0], "conflict")
        self.assertEqual(self.write(self.a, "decide", params)[0], "applied")
        self.assertIn("short progress", self.view(self.b)["memories"]["USER.md"])
        self.assertNotIn("MEMORY.md", self.view(None)["memories"])

    def test_clear_is_versioned_and_replay_cannot_revive_removed_memory(self):
        entry = "Temporary decision to be removed."
        self.assertEqual(self.write(self.a, "memory", {"action": "add", "content": entry}, digest([]))[0], "applied")
        old = self.view(self.a)["memory_versions"]["memory"]
        self.assertEqual(self.write(self.a, "clear", {"target":"memory","confirmed":True}, old)[0], "applied")
        self.assertNotIn("MEMORY.md", self.view(self.a)["memories"])
        self.assertEqual(self.write(self.a, "memory", {"action": "add", "content": entry}, digest([]))[0], "failed")

    def test_confirmed_clear_removes_all_entries_in_only_the_requested_scope(self):
        from hermes_codex_learning.shared_storage import native_scope, read
        from tools.memory_tool import load_on_disk_store

        project_entries = ["Local preview uses the cobalt palette.", "Replay seed is recorded with each pass."]
        user_entries = ["Prefer short progress updates.", "Prefer the official workflow."]
        other_entry = "Other workspace uses separate validation."
        for content in project_entries:
            version = self.view(self.a)["memory_versions"]["memory"]
            state, _ = self.write(self.a, "memory", {"action": "add", "content": content}, version)
            self.assertEqual(state, "applied")
        self.assertEqual(self.write(self.b, "memory", {"action": "add", "content": other_entry}, digest([]))[0], "applied")
        for content in user_entries:
            version = self.view(None)["memory_versions"]["user"]
            state, staged = self.write(self.a, "memory", {"action": "add", "target": "user", "content": content}, version)
            self.assertEqual(state, "pending_review")
            pending = read(self.bridge, self.bridge.store, "pending", self.a, {})["records"]
            proposal = next(item for item in pending if item["id"] == staged["pending_id"])
            decision = {"subsystem": "memory", "id": proposal["id"], "revision": proposal["revision"], "decision": "approve"}
            self.assertEqual(self.write(self.a, "decide", decision)[0], "applied")
        original_user = self.view(None)["memories"]["USER.md"]
        with native_scope(self.bridge.settings, self.a):
            ordinary_batch = [{"action": "remove", "old_text": entry, "matched_entry": entry} for entry in project_entries]
            result = load_on_disk_store().apply_batch("memory", ordinary_batch)
        self.assertFalse(result["success"])
        self.assertIn("Refusing to empty", result["error"])

        version = self.view(self.a)["memory_versions"]["memory"]
        self.assertEqual(self.write(self.a, "clear", {"target": "memory", "confirmed": True}, digest([]))[0], "conflict")
        self.assertEqual(self.write(self.a, "clear", {"target": "memory", "confirmed": True}, version)[0], "applied")
        self.assertNotIn("MEMORY.md", self.view(self.a)["memories"])
        self.assertEqual(self.view(self.b)["memories"]["MEMORY.md"], other_entry)
        self.assertEqual(self.view(None)["memories"]["USER.md"], original_user)
        for content in project_entries:
            self.assertEqual(self.write(self.a, "memory", {"action": "add", "content": content}, digest([]))[0], "failed")

        version = self.view(None)["memory_versions"]["user"]
        self.assertEqual(self.write(self.a, "clear", {"target": "user", "confirmed": True}, version)[0], "applied")
        self.assertNotIn("USER.md", self.view(None)["memories"])
        self.assertEqual(self.view(self.b)["memories"]["MEMORY.md"], other_entry)

    def test_owner_recovery_records_delete_before_reviewing_a_restored_file(self):
        from hermes_codex_learning.shared_storage import _intent_key, memory_path, native_scope, read, recover_memory_intents
        from tools.memory_tool import load_on_disk_store

        entry = "A replaced workflow constraint."
        path = memory_path(self.bridge.settings, self.a, "memory")
        self.assertEqual(self.write(self.a, "memory", {"action":"add", "content":entry}, digest([]))[0], "applied")
        key = _intent_key(self.a, "memory")
        intent = {"project":self.a,"target":"memory","before":digest([entry]),"after":digest([]),
                  "removed":[digest(entry)]}
        self.bridge.store.set(key, intent)
        recover_memory_intents(self.bridge.settings, self.bridge.store)
        self.assertIn(entry, self.view(self.a)["memories"]["MEMORY.md"])
        self.bridge.store.set(key, intent)
        path.write_text("")
        recover_memory_intents(self.bridge.settings, self.bridge.store)
        path.write_text(entry)
        self.assertNotIn("MEMORY.md", self.view(self.a)["memories"])
        self.assertFalse(read(self.bridge, self.bridge.store, "journey", self.a, {})["memory"])
        with native_scope(self.bridge.settings, self.a):
            memory = load_on_disk_store()
            self.assertFalse(memory._entries_for("memory"))
        self.assertEqual(self.write(self.a, "memory", {"action":"add", "content":entry}, digest([]))[0], "failed")

    def test_journey_deletion_requires_exact_native_node_and_version(self):
        try:
            import agent.shared_learning_client  # noqa: F401
        except ImportError:
            self.skipTest("Cross-agent Hermes client/journey forwarding removed in slimmed backend")
        from hermes_codex_learning.shared_storage import read

        entry = "Use the proof palette when reviewing the fixture report."
        self.assertEqual(self.write(self.a, "memory", {"action":"add", "content":entry}, digest([]))[0], "applied")
        graph = read(self.bridge, self.bridge.store, "journey", self.a, {})
        node, = [row for row in graph["nodes"] if row["kind"] == "memory"]
        self.assertEqual(read(self.bridge, self.bridge.store, "journey", self.a, {"id":node["id"]})["content"], entry)
        self.assertFalse(read(self.bridge, self.bridge.store, "journey", self.b, {"id":node["id"]})["ok"])
        self.assertEqual(self.write(self.a, "journey_delete", {"id":node["id"]}, digest([]))[0], "conflict")
        self.assertEqual(self.write(self.a, "journey_delete", {"id":node["id"]}, node["version"])[0], "applied")
        self.assertFalse(read(self.bridge, self.bridge.store, "journey", self.a, {"id":node["id"]})["ok"])

    def test_client_outbox_has_private_permissions_and_replays_in_order(self):
        try:
            from agent.shared_learning_client import Client, Refused
        except ImportError:
            self.skipTest("Cross-agent Hermes client forwarding removed in slimmed backend")
        path = self.bridge.settings.shared_profile
        path.mkdir()
        client = Client(path, str(self.root / "offline.sock"), "a" * 48)
        client.queue("event", {"kind":"prompt"})
        client.queue("event", {"kind":"finished"})
        client.flush()
        def owner_starting(*_):
            raise Refused("owner still starting")
        client.flush(owner_starting)
        observed = []
        client.flush(lambda method, params: observed.append(params["kind"]))
        self.assertEqual(observed, ["prompt", "finished"])
        self.assertFalse((path / "shared-learning-outbox.sqlite").stat().st_mode & 0o077)
