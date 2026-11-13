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
