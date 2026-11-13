"""User requests bind real execution evidence and survive bridge restarts."""
import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.normalize import merge_event
from hermes_codex_learning.store import digest
from hermes_codex_learning import learning_dispatch


class ScriptedAPI:
    """Deterministic provider boundary; state/queue/publication run through Bridge."""
    def __init__(self):
        self.intents = {}
        self.attempts = {}
        self.snapshots = []
        self.calls = 0

    def open(self, source):
        return {"handle": "handle-" + source["chat"]}

    def request_review(self, handle, **request):
        key = request["request_id"]
        if key not in self.intents:
            self.snapshots.append(request)
            self.intents[key] = {"id":key, "request_id":key, "handle":handle, "reason":"user_request", "status":"queued"}
        return self.intents[key]

    def read(self, *, kind, name=None, **kwargs):
        if kind == "context":
            return {"memories": {}, "skills": [], "version":"fixture"}
        if kind == "request":
            return {"intent":self.intents[name], "attempt":self.attempts.get(name)}
        return {"intents":[v for v in self.intents.values() if v["status"] in {"queued","unknown"}], "attempts":[]}

    def run_review(self, handle, intent, *, attempt_id):
        if intent not in self.attempts:
            self.calls += 1
            self.attempts[intent] = {"id":attempt_id, "run_status":"completed", "applied":[],"staged":[],"uncertain":[]}
            self.intents[intent]["status"] = "completed"
        return self.attempts[intent]

    def cancel_request(self, request_id):
        self.intents[request_id]["status"] = "cancelled"
        return {"status":"cancelled"}


class ActiveLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.settings = Settings(self.root / "home", self.project, self.root / "native",
                                 Path("/usr/bin/python3"), notifications=False)
        self.bridge = Bridge(self.settings, object())
        self.addCleanup(lambda: self.bridge.store.close())
        self.turn("chat-a", "turn-a", "Alpha task", "Alpha result")
        self.turn("chat-b", "turn-b", "Unrelated task", "Foreign result")

    def turn(self, chat, turn, user, assistant=None):
        for kind, fields in [("UserPromptSubmit", {"prompt": user}),
                             *(([("Stop", {"last_assistant_message": assistant})]) if assistant else [])]:
            event = {"hook_event_name": kind, "cwd": str(self.project), "bridge_project": str(self.project),
                     "bridge_host": self.bridge.store.get("host_id"), "session_id": chat, "turn_id": turn, **fields}
            identity = self.bridge.store.capture(event)
            row = self.bridge.store.db.execute("SELECT * FROM events WHERE id=?", (identity,)).fetchone()
            data = merge_event(self.bridge.store, dict(row))
        if assistant:
            with self.bridge.store.db:
                self.bridge.store.db.execute("UPDATE turns SET observed_revision=revision WHERE id=?", (digest([chat, turn]),))
        return data

    def request(self, **extra):
        return self.bridge.rpc("request_learning", {"cwd": str(self.project), "chat_id": "chat-a",
            "request_id": "request-a", "focus": "Capture the verified method", **extra})

    def test_learn_can_target_an_existing_native_skill_without_creating_a_duplicate(self):
        self.bridge.store.set("artifact-read-cache", {"codebase-inspection": {"artifact": {"version": "v1"}}})
        result = self.request(mode="learn", skill_name="codebase-inspection")
        self.assertEqual(result["state"], "collecting")
        self.assertEqual(result["skill_name"], "codebase-inspection")
        self.bridge.store.set("artifact-read-cache", {})
        self.assertEqual(self.request(mode="learn", skill_name="codebase-inspection"), result)

    def test_learn_rejects_unknown_unprefixed_or_unsafe_skill_names(self):
        for name in ("unknown-skill", "../codebase-inspection", "a/b", "learned-", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.request(mode="learn", skill_name=name)

    def test_request_is_durable_idempotent_and_scoped_to_the_selected_chat(self):
        result = self.request()
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["evidence"]["turns"], ["turn-a"])
        self.assertEqual(self.request(), result)
        with self.assertRaises(ValueError):
            self.request(focus="A different request under the same ID")
        self.bridge.store.close()
        self.bridge = Bridge(self.settings, object())
        restored = self.bridge.rpc("learning_request_status", {"cwd": str(self.project), "request_id": "request-a"})
        self.assertEqual(restored["state"], "queued")
        self.assertEqual(restored["evidence"]["turns"], ["turn-a"])
        self.assertNotIn("Foreign result", json.dumps(restored))

    def status(self):
        return self.bridge.rpc("learning_request_status", {"cwd":str(self.project), "request_id":"request-a"})

    def cycle(self):
        learning_dispatch.prepare(self.bridge)
        self.bridge.reviews()
        self.bridge.last_publish = 0
        self.bridge.publish()

    def test_manual_execution_uses_only_the_frozen_evidence_and_replays_no_model_call(self):
        api = self.bridge.api = ScriptedAPI()
        self.request(scope="skills")
        self.turn("chat-a", "future-turn", "Later task", "Must not enter the frozen snapshot")
        self.cycle()
        self.assertEqual(self.status()["state"], "completed")
        self.assertEqual(api.calls, 1)
        snapshot = json.dumps(api.snapshots[0])
        self.assertIn("Alpha result", snapshot)
        self.assertNotIn("Foreign result", snapshot)
        self.assertNotIn("Must not enter", snapshot)
        self.assertFalse(api.snapshots[0]["memory"])
        self.assertEqual(api.snapshots[0]["focus"], "Capture the verified method")
        self.cycle()
        self.assertEqual(api.calls, 1)
        self.request(request_id="new-explicit-request", focus="Learn another aspect")
        self.cycle()
        self.assertEqual(api.calls, 2)

    def test_waits_for_selected_turn_and_honors_worker_pause(self):
        api = self.bridge.api = ScriptedAPI()
        self.turn("chat-a", "running", "Still executing")
        self.assertEqual(self.request(through_turn="running")["state"], "waiting_for_turn")
        learning_dispatch.prepare(self.bridge)
        self.assertEqual(api.calls, 0)
        self.turn("chat-a", "running", "Still executing", "Now finished")
        self.bridge.resume.clear()
        learning_dispatch.prepare(self.bridge)
        self.assertFalse(api.intents)
        self.bridge.resume.set()
        self.cycle()
        self.assertEqual(self.status()["state"], "completed")
        self.assertIn("Now finished", json.dumps(api.snapshots))

    def test_pending_native_intent_can_be_cancelled_before_execution(self):
        api = self.bridge.api = ScriptedAPI()
        self.request()
        learning_dispatch.prepare(self.bridge)
        self.bridge.rpc("cancel_learning", {"cwd":str(self.project), "request_id":"request-a"})
        self.bridge.reviews()
        self.assertEqual(self.status()["state"], "cancelled")
        self.assertEqual(api.calls, 0)

    def test_wrong_chat_and_unrelated_project_cannot_supply_evidence_or_read_results(self):
        with self.assertRaises(ValueError):
            self.request(chat_id="not-captured")
        self.request()
        with self.assertRaises(ValueError):
            self.bridge.rpc("learning_request_status", {"cwd":str(self.root / "unrelated"), "request_id":"request-a"})

    def test_source_chunks_are_bounded_immutable_and_sealed_before_execution(self):
        self.bridge.api = ScriptedAPI()
        self.request(mode="learn", skill_name="learned-fixture")
        params = {"cwd":str(self.project), "request_id":"request-a", "chunk_id":"one",
                  "source":"fixture://manual/chapter-1", "title":"First topic", "content":"Evidence text"}
        result = self.bridge.rpc("add_learning_material", params)
        self.assertEqual(result["progress"]["total_batches"], 2)
        self.assertEqual(self.bridge.rpc("add_learning_material", params), result)
        with self.assertRaises(ValueError):
            self.bridge.rpc("add_learning_material", {**params,"content":"changed"})
        with self.assertRaises(ValueError):
            self.bridge.rpc("add_learning_material", {**params,"chunk_id":"too-big","content":"界" * 9000})
        self.cycle()
        self.assertEqual(self.bridge.api.calls, 0)
        self.bridge.rpc("start_learning", {"cwd":str(self.project),"request_id":"request-a"})
        with self.assertRaises(ValueError):
            self.bridge.rpc("add_learning_material", {**params,"chunk_id":"late"})
        self.cycle()
        self.assertIn("Evidence text", json.dumps(self.bridge.api.snapshots[0]))
        self.assertIn("references/", self.bridge.api.snapshots[0]["focus"])

    def test_native_completion_is_recovered_after_a_crash_before_bridge_publication(self):
        api = self.bridge.api = ScriptedAPI()
        self.request()
        learning_dispatch.prepare(self.bridge)
        intent = next(iter(api.intents.values()))
        api.run_review(intent["handle"], intent["id"], attempt_id="persisted-native-attempt")
        self.cycle()
        self.assertEqual(self.status()["state"], "completed")
        self.assertEqual(api.calls, 1)

    def test_unknown_native_outcome_is_reported_without_a_second_execution(self):
        api = self.bridge.api = ScriptedAPI()
        self.request()
        learning_dispatch.prepare(self.bridge)
        intent = next(iter(api.intents.values()))
        api.attempts[intent["id"]] = {"id":"uncertain-attempt", "run_status":"unknown", "applied":[],
                                      "staged":[],"uncertain":["interrupted after provider request"]}
        intent["status"] = "unknown"
        self.cycle()
        self.assertEqual(self.status()["state"], "unknown")
        self.assertEqual(api.calls, 0)
        self.cycle()
        self.assertEqual(api.calls, 0)

    def test_terminal_hook_waits_for_projection_and_freezes_its_cutoff(self):
        api = self.bridge.api = ScriptedAPI()
        with self.bridge.store.db:
            self.bridge.store.db.execute("UPDATE turns SET observed_revision=NULL WHERE id=?", (digest(["chat-a","turn-a"]),))
        self.assertEqual(self.request()["state"], "waiting_for_turn")
        self.turn("chat-a", "future", "Future input", "Future output")
        with self.bridge.store.db:
            self.bridge.store.db.execute("UPDATE turns SET observed_revision=revision WHERE id=?", (digest(["chat-a","turn-a"]),))
        self.cycle()
        self.assertEqual(self.status()["evidence"]["turns"], ["turn-a"])
        self.assertNotIn("Future output", json.dumps(api.snapshots))

    def test_missing_reference_is_a_failed_learning_result_not_false_completion(self):
        self.bridge.api = ScriptedAPI()
        self.request(mode="learn", skill_name="learned-fixture")
        self.bridge.rpc("add_learning_material", {"cwd":str(self.project), "request_id":"request-a",
            "chunk_id":"one", "source":"fixture://source", "content":"Source rule"})
        self.bridge.rpc("start_learning", {"cwd":str(self.project), "request_id":"request-a"})
        self.cycle()
        self.cycle()
        self.assertEqual(self.status()["state"], "failed")
        self.assertEqual(self.status()["reason"], "reference_index_incomplete")

    def test_retention_removes_completed_source_bodies_but_preserves_request_identity(self):
        from hermes_codex_learning.learning_requests import retire
        self.bridge.api = ScriptedAPI()
        self.request()
        self.cycle()
        result = retire(self.bridge.store, float("inf"), dry_run=False)
        self.assertEqual(result["retired_requests"], 1)
        row = self.bridge.store.db.execute("SELECT payload FROM learning_requests WHERE id='request-a'").fetchone()[0]
        self.assertNotIn("Alpha result", row)
        self.assertNotIn("Capture the verified method", row)
        self.assertTrue(self.request()["body_retired"])
        self.cycle()
        self.assertEqual(self.bridge.api.calls, 1)

    def test_cancel_between_native_completion_and_publication_preserves_the_outcome(self):
        api = self.bridge.api = ScriptedAPI()
        self.request()
        learning_dispatch.prepare(self.bridge)
        self.bridge.reviews()
        self.assertEqual(self.status()["state"], "publishing")
        self.bridge.rpc("cancel_learning", {"cwd":str(self.project),"request_id":"request-a"})
        self.bridge.last_publish = 0
        self.bridge.publish()
        self.assertEqual(self.status()["state"], "cancelled")
        self.assertEqual(len(self.status()["results"]), 1)
        self.assertEqual(api.calls, 1)


if __name__ == "__main__":
    unittest.main()
