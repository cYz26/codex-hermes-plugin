import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_codex_learning.review_metrics import daily
from hermes_codex_learning.store import Store


class ReviewMetricsTests(unittest.TestCase):
    def test_local_day_chat_labels_actual_changes_and_missing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.close)
            first = datetime(2026, 11, 12, 16, tzinfo=timezone.utc).timestamp()

            def insert(identity, created, kind="review.finished", project="/a", **event):
                with store.db:
                    store.db.execute("INSERT INTO notices(id,payload,created) VALUES (?,?,?)",
                                     (identity, json.dumps({"kind": kind, "project": project, **event}), created))

            insert("old:result", first - 1, source_chat="accept")
            insert("acc:start", first - 5, kind="review.started")
            insert("acc:result", first + 10, source_chat="accept", run_status="completed",
                   executor="Claude Code", reason="native_threshold",
                   usage={"api_calls": 3, "input_tokens": 150}, applied_entities=["skill:csv"])
            insert("audit:result", first + 50, source_chat="audit", run_status="failed")
            insert("ordinary:result", first + 51, source_chat="ordinary", run_status="completed",
                   applied_entities=[], usage={"input_tokens": 0}, started_at=first + 10, finished_at=first + 20)
            insert("foreign:result", first + 52, project="/b", source_chat="accept")
            insert("next:result", first + 86400, source_chat="accept")

            groups = daily(store, "/a", "2026-11-13", "Asia/Shanghai",
                           acceptance_chats=["accept"], audit_chats=["audit"])["groups"]
            self.assertEqual(groups["acceptance"]["attempts"], 1)
            self.assertEqual(groups["acceptance"]["totals"], {"api_calls": 3, "input_tokens": 150, "wall_seconds": 15})
            self.assertEqual(groups["acceptance"]["entities"], ["skill:csv"])
            self.assertEqual(groups["acceptance"]["wall_time_sources"], {"notice_interval": 1})
            self.assertEqual(groups["audit"]["outcomes"], {"failed": 1})
            self.assertEqual(groups["audit"]["coverage"], {})
            self.assertEqual(groups["other"]["attempts_with_changes"], 0)
            self.assertEqual(groups["other"]["totals"], {"input_tokens": 0, "wall_seconds": 10})
            self.assertEqual(groups["other"]["wall_time_sources"], {"worker_run": 1})
            self.assertEqual(groups["acceptance"]["by_reason"]["native_threshold"]["totals"]["api_calls"], 3)
            self.assertEqual(groups["acceptance"]["by_executor"]["Claude Code"]["coverage"]["input_tokens"], 1)
            self.assertEqual(groups["audit"]["by_executor"]["unknown"]["totals"], {})

    def test_skill_evidence_matches_exact_delivery_calls_and_versions(self):
        from hermes_codex_learning.store import dumps

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.close)
            day = datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp()

            def deliver(identity, source, version, at, project="/a"):
                store.set("issued-delivery:" + identity, {"project": project, "source_event": source,
                          "name": "fixture", "version": version, "confirmed_at": at})

            def apply(identity, source, version, validation, *, application="self_reported", project="/a"):
                evidence = {"project": project, "delivery_source_event": source, "name": "fixture",
                            "version": version, "application": application, "validation": validation,
                            "test_outcomes": [{"outcome": "completed"}]}
                with store.db:
                    store.db.execute("INSERT INTO application_evidence VALUES (?,?,?)",
                                     (identity, dumps(evidence), day + 86400))

            first, second, old = (["chat", "turn", call] for call in ("first", "second", "old"))
            deliver("body", first, "v2", day + 1)
            deliver("resource", first, "v2", day + 2)
            deliver("second", second, "v2", day + 3)
            deliver("previous", old, "v1", day - 1)
            deliver("undated", old, "v2", None)
            deliver("foreign", old, "v2", day + 4, project="/b")
            apply("adoption", first, "v2", "not_supplied")
            apply("valid", first, "v2", "host_test_passed")
            apply("repeated", first, "v2", "host_test_passed")
            apply("inferred", second, "v2", "host_test_passed", application="host_observed")
            apply("superseded", second, "v2", "superseded")
            apply("wrong-version", second, "v1", "host_test_failed")
            apply("wrong-project", second, "v2", "host_test_passed", project="/b")

            evidence = daily(store, "/a", "2026-11-13", "UTC")["skill_evidence"]
            self.assertEqual(evidence["undated_confirmed_deliveries"], 1)
            self.assertEqual(evidence["versions"], [{"name": "fixture", "version": "v2",
                "confirmed_deliveries": 2, "reported_applications": 1, "host_test_passed": 1, "host_test_failed": 0}])
