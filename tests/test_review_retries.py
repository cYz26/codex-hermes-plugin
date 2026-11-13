"""Recovery must execute a new native attempt and retain earlier receipts."""
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import Store, digest


def native_source(name, *, legacy=False):
    root = Path(__file__).resolve().parents[1]
    text = (root / ("patches/native-learning-api-v1.patch" if legacy else
                    "patches/v0.23.0/native-learning-api-v1.patch")).read_text()
    section = re.search(r"(?ms)^diff --git a/" + re.escape(name) +
                        r".*?(?=^diff --git|\Z)", text).group()
    return "\n".join(line[1:] for line in section.splitlines()
                     if line.startswith("+") and not line.startswith("+++")) + "\n"


class FixtureRuntime:
    def __init__(self):
        self.outcomes = []
        self.reviews = []

    def tick(self, handle, source, memory_count, skill_count, **kwargs):
        return {"memory": False, "skills": False, "memory_count": 0, "skill_count": 0}

    def project(self, *args, **kwargs):
        return "not_configured"

    def review(self, handle, source, messages, intent):
        self.reviews.append((messages, intent))
        return self.outcomes.pop(0)

    def maintenance(self, idle_seconds):
        return {}

    def usage(self, handles):
        return None

    def cancel(self):
        pass

    def close(self):
        return {}


class NativeRetryContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        agent = types.ModuleType("agent")
        modules = {
            "agent": agent,
            "agent.learning_artifacts": types.SimpleNamespace(fingerprint=digest),
            "agent.learning_results": types.SimpleNamespace(changed_skill_name=lambda fact: fact.get("name")),
            "agent.learning_runtime": types.SimpleNamespace(NativeLearningRuntime=FixtureRuntime),
            "hermes_cli.plugins_loader": types.SimpleNamespace(_plugin_home_scope=lambda _: contextlib.nullcontext()),
        }
        binding = patch.dict(sys.modules, modules)
        binding.start()
        self.addCleanup(binding.stop)
        namespace = {"__name__": "fixture_learning_service"}
        exec(compile(native_source("agent/learning_service.py"), "learning_service.py", "exec"), namespace)
        self.service = namespace["LearningService"](self.home, "fixture")
        self.addCleanup(self.service.close)
        self.source = {"host": "fixture", "project": str(self.home), "chat": "chat-a"}
        self.handle = self.service.open(self.source)["handle"]
        self.service.observe(self.handle, {
            "source": {**self.source, "turn": "turn-1"}, "user": "Inspect a plugin",
            "assistant": "Inspected and verified", "status": "completed", "tools": [],
        }, event_id="capture-1")
        self.intent = self.service.maintenance(handle=self.handle, idle_flush=True)["intent"]

    def test_failed_attempt_retries_same_snapshot_and_preserves_applied_receipt(self):
        applied = {"tool": "skill_manage", "action": "create", "name": "learned-fixture",
                   "call_id": "write-1", "result": {"success": True}}
        self.service.runtime.outcomes = [
            {"run_status": "failed", "error": "HTTP 429: waiting budget exhausted",
             "applied": [applied], "staged": [], "uncertain": []},
            {"run_status": "completed", "error": None, "applied": [], "staged": [], "uncertain": []},
        ]
        failed = self.service.run_review(self.handle, self.intent, attempt_id="failed-1")
        retry = self.service.retry_review(self.handle, self.intent,
                                          failed_attempt_id="failed-1", max_retries=3)
        self.assertEqual(retry["status"], "queued")
        self.assertNotEqual(retry["intent_id"], self.intent)
        completed = self.service.run_review(self.handle, retry["intent_id"], attempt_id="retry-1")
        self.assertEqual(completed["run_status"], "completed")
        self.assertEqual(self.service.runtime.reviews[0][0], self.service.runtime.reviews[1][0])
        self.assertEqual(self.service.runtime.reviews[1][1]["recovery"]["applied"][0]["name"], "learned-fixture")
        retained = self.service.run_review(self.handle, self.intent, attempt_id="failed-1")
        self.assertEqual(retained["run_status"], "failed")
        self.assertEqual(retained["applied"], failed["applied"])
        attempts = self.service.read(kind="status")["attempts"]
        self.assertEqual({item["id"]: item["run_status"] for item in attempts},
                         {"failed-1": "failed", "retry-1": "completed"})

    def fail_review(self, *, status="failed", error="HTTP 429: waiting budget exhausted", uncertain=()):
        self.service.runtime.outcomes = [{"run_status": status, "error": error,
                                         "applied": [], "staged": [], "uncertain": list(uncertain)}]
        return self.service.run_review(self.handle, self.intent, attempt_id="failed-1")

    def test_retry_request_is_idempotent_and_rejects_an_unconfirmed_write(self):
        self.fail_review()
        first = self.service.retry_review(self.handle, self.intent, failed_attempt_id="failed-1")
        again = self.service.retry_review(self.handle, self.intent, failed_attempt_id="failed-1")
        self.assertEqual(first, again)
        self.assertEqual(len(self.service.read(kind="status")["intents"]), 2)
        with self.assertRaisesRegex(ValueError, "original scope"):
            self.service.retry_review("different-scope", self.intent, failed_attempt_id="failed-1")
        self.service.db.execute("UPDATE attempts SET payload=? WHERE id='failed-1'", (
            json.dumps({"run_status": "failed", "uncertain": ["tool reply was not recorded"]}),))
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.service.retry_review(self.handle, self.intent, failed_attempt_id="failed-1")

    def test_explicit_requests_keep_their_result_and_cannot_enter_automatic_recovery(self):
        intent = self.service.request_review(self.handle, request_id="explicit-fixture",
            messages=[{"role":"user", "content":"Selected learning material"}], focus="Keep the source scope",
            memory=False, skills=True)
        self.service.runtime.outcomes = [{"run_status":"failed", "error":"HTTP 429: limited",
                                         "applied":[], "staged":[], "uncertain":[]}]
        self.service.run_review(self.handle, intent["id"], attempt_id="explicit-failed")
        retained = self.service.read(kind="request", name="explicit-fixture")
        with self.assertRaisesRegex(ValueError, "Explicit learning"):
            self.service.retry_review(self.handle, intent["id"], failed_attempt_id="explicit-failed")
        queue = self.queue()
        self.assertEqual(queue.sync(self.service), [])
        self.assertEqual(queue.summary()["counts"], {})
        self.assertEqual(self.service.read(kind="request", name="explicit-fixture"), retained)

    def test_schema_one_upgrade_keeps_explicit_snapshots_and_cancellation(self):
        self.service.request_review(self.handle, request_id="pending-explicit",
            messages=[{"role":"user", "content":"Preserve this selected source"}], focus="Retain the focus")
        retained = self.service.read(kind="request", name="pending-explicit")
        self.service.db.execute("PRAGMA user_version=1")
        service_type = type(self.service)
        self.service.close()
        self.service = service_type(self.home, "fixture")
        self.addCleanup(self.service.close)
        self.assertEqual(self.service.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.service.read(kind="request", name="pending-explicit"), retained)
        self.assertEqual(self.service.cancel_request("pending-explicit")["status"], "cancelled")

    def test_legacy_owner_refuses_the_recovery_aware_journal(self):
        self.fail_review()
        self.assertEqual(self.service.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.service.close()
        namespace = {"__name__": "fixture_legacy_learning_service"}
        exec(compile(native_source("agent/learning_service.py", legacy=True), "legacy_learning_service.py", "exec"), namespace)
        with self.assertRaisesRegex(ValueError, "Unsupported learning journal schema"):
            namespace["LearningService"](self.home, "fixture")

    def test_a_newer_completed_revision_keeps_old_retry_superseded_after_rescan(self):
        self.fail_review()
        queue = self.queue()
        queue.sync(self.service)
        self.service.observe(self.handle, {
            "source": {**self.source, "turn": "turn-2"}, "user": "Verify the completed change",
            "assistant": "Verified", "status": "completed", "tools": [],
        }, event_id="capture-2")
        newer = self.service.maintenance(handle=self.handle, idle_flush=True)["intent"]
        self.service.runtime.outcomes = [{"run_status": "completed", "uncertain": []}]
        self.service.run_review(self.handle, newer, attempt_id="newer")
        self.now = 1300.0
        self.assertEqual(queue.process(self.service), [])
        self.assertEqual(queue.summary()["counts"], {"superseded": 1})
        queue.sync(self.service, force=True)
        self.assertEqual(queue.summary()["counts"], {"superseded": 1})

    def test_recovery_read_pages_include_the_original_scope_and_receipt(self):
        self.fail_review()
        page = self.service.read(kind="review_recovery", after_cursor=0)
        self.assertEqual(len(page["items"]), 1)
        item = page["items"][0]
        self.assertEqual(item["source"], self.source)
        self.assertEqual(item["attempt"]["id"], "failed-1")
        self.assertEqual(item["intent"]["id"], self.intent)
        self.assertFalse(page["has_more"])
        self.assertEqual(self.service.read(kind="review_recovery", after_cursor=page["next_cursor"])["items"], [])

    def queue(self):
        from hermes_codex_learning.review_retries import ReviewRetryQueue
        settings = Settings(self.home, self.home, self.home, Path("/usr/bin/python3"))
        store = Store(settings.state)
        self.addCleanup(store.close)
        self.now = 1000.0
        return ReviewRetryQueue(settings, store, clock=lambda: self.now)

    def test_old_failure_is_discovered_and_cooldown_survives_queue_restart(self):
        self.fail_review()
        queue = self.queue()
        queue.sync(self.service)
        self.assertEqual(queue.summary()["counts"], {"waiting": 1})
        self.assertEqual(queue.process(self.service), [])
        self.now = 1299.0
        queue.sync(self.service)
        from hermes_codex_learning.review_retries import ReviewRetryQueue
        recovered = ReviewRetryQueue(queue.settings, queue.store, clock=lambda: self.now)
        self.assertEqual(recovered.process(self.service), [])
        self.now = 1300.0
        dispatched = recovered.process(self.service)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["retry_index"], 1)
        self.assertEqual(recovered.process(self.service), [])
        self.service.runtime.outcomes = [{"run_status": "completed", "applied": [], "staged": [], "uncertain": []}]
        self.service.run_review(self.handle, dispatched[0]["intent_id"], attempt_id="retry-1")
        recovered.sync(self.service, force=True)
        self.assertEqual(recovered.summary()["counts"], {"completed": 1})

    def test_stored_ttfb_failure_enters_cooldown_without_replaying_the_attempt(self):
        error = "Codex stream produced no parsed stream event within 497s (TTFB threshold: 300s)"
        self.fail_review(error=error)
        queue = self.queue()
        # Reproduce the queue row created by the previous worker build.
        with patch("hermes_codex_learning.review_retries.retry_reason", return_value="non_transient"):
            queue.sync(self.service)
        self.assertEqual(queue.summary()["counts"], {"blocked": 1})

        queue.sync(self.service, force=True)
        retry, = queue.summary()["items"]
        self.assertEqual((retry["state"], retry["reason"], retry["next_retry_at"]),
                         ("waiting", "connection", 1300.0))
        self.assertEqual(queue.process(self.service), [])
        self.now = 1300.0
        successor, = queue.process(self.service)
        self.assertEqual(queue.process(self.service), [])
        self.assertNotEqual(successor["intent_id"], self.intent)
        original = self.service.run_review(self.handle, self.intent, attempt_id="failed-1")
        self.assertEqual((original["run_status"], original["error"]), ("failed", error))
        self.assertEqual(len(self.service.runtime.reviews), 1)

    def test_three_retries_back_off_and_then_stop(self):
        self.fail_review()
        queue = self.queue()
        queue.sync(self.service)
        for index, due in enumerate((1300.0, 1900.0, 3100.0), 1):
            self.assertEqual(queue.summary()["items"][0]["next_retry_at"], due)
            self.now = due
            item = queue.process(self.service)[0]
            self.service.runtime.outcomes = [{"run_status": "failed", "error": "HTTP 429: limited",
                                             "applied": [], "staged": [], "uncertain": []}]
            self.service.run_review(self.handle, item["intent_id"], attempt_id=f"retry-{index}")
            queue.sync(self.service)
        self.assertEqual(queue.summary()["counts"], {"exhausted": 1})
        self.now = 10000.0
        self.assertEqual(queue.process(self.service), [])
        self.assertEqual(len(self.service.runtime.reviews), 4)

    def test_auth_failure_and_unknown_execution_are_not_replayed(self):
        self.fail_review(error="HTTP 403: access denied")
        queue = self.queue()
        queue.sync(self.service)
        self.now = 10000.0
        self.assertEqual(queue.process(self.service), [])
        self.assertEqual(queue.summary()["items"][0]["reason"], "non_transient")
        self.service.db.execute("UPDATE attempts SET status='unknown',payload=? WHERE id='failed-1'", (
            json.dumps({"run_status": "unknown", "error": "TimeoutError", "uncertain": ["executor raised"]}),))
        queue.sync(self.service)
        self.assertEqual(queue.process(self.service), [])
        self.assertEqual(queue.summary()["items"][0]["reason"], "unconfirmed_outcome")

    def test_reviewed_unknown_write_stays_closed_without_changing_native_history(self):
        fact = {"tool": "skill_manage", "call_id": "missing-result"}
        self.fail_review(status="interrupted", error=None, uncertain=[fact])
        bridge = self.bridge()
        bridge.reviews()
        params = {"attempt_id": "failed-1", "close_without_retry": True,
                  "note": "The expected changes are already present.",
                  "evidence": "/audit/attempt-reconciliation.md"}
        preview = bridge.rpc("review_retry", params)
        self.assertTrue(preview["can_close_without_retry"])
        self.assertEqual(preview["state"], "blocked")
        applied = bridge.rpc("review_retry", {**params, "dry_run": False})
        self.assertEqual(applied["state"], "closed_without_retry")
        self.assertTrue(applied["applied"])
        bridge.retry_queue.sync(self.service, force=True)
        self.assertEqual(bridge.retry_queue.summary()["counts"], {"closed_without_retry": 1})
        self.assertEqual(bridge.retry_queue.process(self.service), [])
        original = self.service.run_review(self.handle, self.intent, attempt_id="failed-1")
        self.assertEqual((original["run_status"], original["uncertain"]), ("interrupted", [fact]))
        self.assertEqual(len(self.service.runtime.reviews), 1)

    def bridge(self, **settings_overrides):
        from hermes_codex_learning.daemon import Bridge
        def read(**kwargs):
            if kwargs["kind"] == "context":
                return {"version": "fixture", "summary": "", "memories": {}, "skills": []}
            return self.service.read(**kwargs)
        api = types.SimpleNamespace(version=1, read=read, retry_review=self.service.retry_review,
                                    run_review=self.service.run_review, cancel=self.service.cancel)
        settings = Settings(self.home, self.home, self.home, Path("/usr/bin/python3"), **settings_overrides)
        bridge = Bridge(settings, api)
        self.addCleanup(bridge.store.close)
        self.now = 1000.0
        bridge.retry_queue.clock = lambda: self.now
        return bridge

    def test_review_deadline_cancels_the_whole_attempt_and_keeps_confirmed_writes(self):
        cancelled = threading.Event()
        write = {"tool": "skill_manage", "action": "patch", "name": "fixture-skill",
                 "call_id": "confirmed-before-timeout", "result": {"success": True}}

        def review(*_):
            state = "interrupted" if cancelled.wait(3) else "completed"
            return {"run_status": state, "applied": [write], "uncertain": []}

        self.service.runtime.review = review
        self.service.runtime.cancel = cancelled.set
        bridge = self.bridge(review_timeout_seconds=1)
        bridge.reviews()

        attempt, = self.service.read(kind="status")["attempts"]
        self.assertTrue(cancelled.is_set())
        self.assertEqual((attempt["run_status"], attempt["applied"]), ("interrupted", [write]))
        for _ in range(2):
            bridge.retry_queue.sync(self.service, force=True)
        retry, = bridge.retry_queue.summary()["items"]
        self.assertEqual((retry["state"], retry["reason"], retry["next_retry_at"]),
                         ("blocked", "review_deadline", None))
        self.assertEqual(bridge.retry_queue.process(self.service), [])

    def test_hard_deadline_recovers_an_unacknowledged_review_as_unknown(self):
        self.service.close()
        # Run a separate owner so the real hard stop cannot kill the test runner.
        script = '''
from pathlib import Path
import sys
import time
from test_review_retries import NativeRetryContracts

fixture = NativeRetryContracts()
fixture.setUp()
service_type = type(fixture.service)
fixture.service.close()
fixture.service = service_type(Path(sys.argv[1]), "fixture")
fixture.home = fixture.service.home
fixture.service.runtime.review = lambda *_: time.sleep(7)
fixture.service.runtime.cancel = lambda: time.sleep(7)
from hermes_codex_learning import review_deadline
review_deadline.CANCEL_GRACE_SECONDS = 0.5
fixture.bridge(review_timeout_seconds=1).reviews()
'''
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.home)],
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (root / "src", root / "tests")))},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 124, result.stderr)
        service_type = type(self.service)
        self.service = service_type(self.home, "fixture")
        self.addCleanup(self.service.close)
        attempt, = self.service.read(kind="status")["attempts"]
        self.assertEqual(attempt["run_status"], "unknown")
        self.assertEqual(attempt["id"], self.service.run_review(self.handle, self.intent, attempt_id=attempt["id"])["id"])
        queue = self.queue()
        queue.sync(self.service, force=True)
        retry, = queue.summary()["items"]
        self.assertEqual((retry["state"], retry["reason"], retry.get("failure_reason")),
                         ("blocked", "unconfirmed_outcome", "review_deadline"))
        self.assertEqual(queue.process(self.service), [])

    def test_owner_loop_runs_the_due_retry_and_reports_its_completion(self):
        self.fail_review()
        bridge = self.bridge()
        bridge.reviews()
        self.assertEqual(len(self.service.runtime.reviews), 1)
        self.assertEqual(bridge.rpc("learning_status", {})["review_retries"]["counts"], {"waiting": 1})
        self.now = 1300.0
        self.service.runtime.outcomes = [{"run_status": "completed", "applied": [], "staged": [], "uncertain": []}]
        bridge.reviews()
        self.assertEqual(len(self.service.runtime.reviews), 2)
        self.assertEqual(bridge.retry_queue.summary()["counts"], {"completed": 1})
        notices = [json.loads(row[0]) for row in bridge.store.db.execute("SELECT payload FROM notices ORDER BY seq")]
        self.assertIn("自动重试", notices[0]["message"])
        self.assertIn("重试完成", notices[-1]["message"])
        from hermes_codex_learning.notify import should_notify
        self.assertTrue(should_notify(notices[-1]))
        self.now = 2000.0
        bridge.reviews()
        self.assertEqual(len(self.service.runtime.reviews), 2)

    def test_manual_retry_is_previewable_and_uses_the_same_bounded_queue(self):
        self.fail_review()
        bridge = self.bridge()
        bridge.reviews()
        preview = bridge.rpc("review_retry", {"attempt_id": "failed-1", "dry_run": True})
        self.assertTrue(preview["eligible"])
        self.assertFalse(preview["applied"])
        self.assertEqual(bridge.retry_queue.summary()["items"][0]["next_retry_at"], 1300.0)
        with patch("hermes_codex_learning.review_retries.time.time", return_value=1000.0):
            result = bridge.rpc("review_retry", {"attempt_id": "failed-1", "dry_run": False})
        self.assertTrue(result["applied"])
        self.assertEqual(bridge.retry_queue.summary()["items"][0]["next_retry_at"], 1000.0)
        self.assertEqual(len(self.service.runtime.reviews), 1)


class RetryPolicyContracts(unittest.TestCase):
    def test_transient_failures_retry_but_budget_and_auth_failures_stop(self):
        from hermes_codex_learning.review_input import BUDGET_EXHAUSTED
        from hermes_codex_learning.review_retries import retry_reason
        cases = (("HTTP 429: limited", "rate_limit"), ("HTTP 503: unavailable", "provider_unavailable"),
                 ("APIConnectionError: peer reset", "connection"), ("ReadTimeout: timed out", "connection"),
                 ("ConnectionResetError: peer closed", "connection"), ("ConnectError: connection failed", "connection"),
                 ("RemoteProtocolError: peer closed", "connection"), ("BrokenPipeError", "connection"),
                 ("HTTP 401: unauthorized", "non_transient"), ("HTTP 403: denied", "non_transient"))
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(retry_reason({"run_status": "failed", "error": error}), expected)
        self.assertEqual(retry_reason({"run_status": "interrupted", "error": None}), "interrupted")
        self.assertEqual(retry_reason({"run_status": "interrupted", "error": "HTTP 403: denied"}), "non_transient")
        self.assertEqual(retry_reason({"run_status": "failed", "error": "Timeout", "failure_reason": BUDGET_EXHAUSTED}), "non_transient")

    def test_retry_policy_rejects_unbounded_or_nonfinite_values(self):
        base = (Path("/tmp/home"), Path("/tmp/project"), Path("/tmp/source"), Path("/usr/bin/python3"))
        for changes in ({"review_retry_limit": True}, {"review_retry_limit": 11},
                        {"review_retry_delay": float("nan")}, {"review_retry_delay": True},
                        {"review_retry_delay": 0}, {"review_retry_max_delay": 299},
                        {"review_retry_enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(*base, **changes)
