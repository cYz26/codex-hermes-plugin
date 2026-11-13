import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_codex_learning.daily_audit import export, mark_reviewed
from hermes_codex_learning.store import Store


class DailyAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.out = Path(self.tmp.name) / "reports"
        self.store = Store(self.home / "bridge")
        self.addCleanup(self.store.close)
        (self.home / "learning").mkdir()
        self.native = sqlite3.connect(self.home / "learning/journal.sqlite")
        self.addCleanup(self.native.close)
        self.native.executescript('''
            CREATE TABLE attempts(id TEXT, intent TEXT, status TEXT, payload TEXT);
            CREATE TABLE intents(id TEXT, handle TEXT, status TEXT, payload TEXT);
            CREATE TABLE scopes(id TEXT, source TEXT);
        ''')
        self.native.execute("INSERT INTO scopes VALUES (?,?)", ("scope", json.dumps({"project": "/a", "chat": "chat"})))
        self.native.commit()
        self.now = datetime(2026, 11, 14, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def attempt(self, identity, state="completed"):
        with self.native:
            self.native.execute("INSERT INTO intents VALUES (?,?,?,?)", (identity, "scope", state, "{}"))
            self.native.execute("INSERT INTO attempts VALUES (?,?,?,?)", (identity, identity, state,
                json.dumps({"run_status": state, "applied": [], "uncertain": []})))

    def notice(self, identity, at, **extra):
        with self.store.db:
            self.store.db.execute("INSERT INTO notices(id,payload,created) VALUES (?,?,?)", (
                identity + ":result", json.dumps({"kind": "review.finished", "intent_id": identity,
                "project": "/a", "source_chat": "chat", "run_status": "completed", **extra}), at))

    def run_export(self, day="2026-11-13", **kw):
        return export(self.home, self.out, day, now=self.now, **kw)

    def test_readonly_complete_ledger_day_boundary_and_unknown_time(self):
        self.attempt("dated")
        self.attempt("unknown")
        edge = datetime(2026, 11, 14, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.notice("dated", edge, started_at=edge - 100, finished_at=edge)
        before = self.native.execute("SELECT * FROM attempts").fetchall()
        report = self.run_export()
        self.assertEqual(report["window_attempts"], [])
        self.assertEqual(report["undated_attempts"], ["unknown"])
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(report["state"], "partial")
        self.assertEqual(self.native.execute("SELECT * FROM attempts").fetchall(), before)
        next_day = self.run_export("2026-11-14")
        self.assertEqual(next_day["window_attempts"], ["dated"])
        self.assertIsNone(next_day["attempts"]["dated"]["paid_cost"])
        self.assertTrue((self.out / "2026-11-14.md").is_file())

    def test_incremental_late_completion_version_diff_and_repeat(self):
        self.attempt("late", "running")
        self.store.set("artifact-read-cache", {"example": {"artifact": {"version": "v1", "files": {"SKILL.md": "old"}}}})
        first = self.run_export()
        self.assertEqual(first["collection"]["native_rows_read"], 1)
        with self.native:
            self.native.execute("UPDATE attempts SET status='completed',payload=? WHERE id='late'", (json.dumps({"applied": [], "run_status": "completed"}),))
        self.notice("late", self.now, finished_at=self.now, started_at=self.now - 3)
        self.store.set("artifact-read-cache", {"example": {"artifact": {"version": "v2", "files": {"SKILL.md": "new"}}}})
        second = self.run_export("2026-11-14")
        self.assertEqual(second["attempts"]["late"]["state"], "completed")
        self.assertEqual(second["attempts"]["late"]["first_report_date"], "2026-11-13")
        self.assertEqual(second["artifact_changes"][0]["previous_version"], "v1")
        repeated = self.run_export("2026-11-14")
        self.assertEqual(repeated["collection"]["native_rows_read"], 0)
        self.assertEqual(repeated["artifact_changes"], second["artifact_changes"])
        self.assertEqual(repeated["window_attempts"], ["late"])
        checkpoint = json.loads((self.out / "collection.json").read_text())
        self.assertIsNone(checkpoint["last_completed_review_date"])

    def test_missing_source_still_delivers_partial_report_without_creating_database(self):
        missing = self.home / "missing"
        result = export(missing, self.out, "2026-11-13", now=self.now)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(result["gaps"]), 2)
        self.assertFalse(missing.exists())
        self.assertTrue((self.out / "2026-11-13.md").exists())

    def test_deadline_produces_partial_report_without_advancing_success_cursor(self):
        self.attempt("pending", "running")
        self.run_export()
        before = (self.out / "collection.json").read_bytes()
        result = self.run_export(timeout=0)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(any('TimeoutError' in gap['reason'] for gap in result['gaps']))
        self.assertEqual((self.out / "collection.json").read_bytes(), before)

    def test_report_cannot_overwrite_knowledge_and_separates_task_classes(self):
        self.attempt('audit')
        self.notice('audit', self.now - 86400, finished_at=self.now - 86400)
        with self.assertRaises(ValueError):
            export(self.home, self.home / 'skills', '2026-11-13')
        report = self.run_export(audit_chats=['chat'])
        self.assertEqual(report['attempts']['audit']['task_class'], 'audit')
        self.assertEqual(report['metrics']['/a']['groups']['audit']['attempts'], 1)
        with self.assertRaises(ValueError):
            self.run_export(audit_chats=['chat'], acceptance_chats=['chat'])

    def test_late_disposition_closes_tracking_without_rewriting_native_unknown(self):
        self.attempt('old', 'interrupted')
        with self.store.db:
            self.store.db.execute('INSERT INTO review_retries VALUES (?,?,?,?,?,?)',
                ('old', 'scope', 'closed_without_retry', None,
                 json.dumps({'root_intent': 'old', 'last_attempt': 'old', 'disposition': {'evidence': 'audit-note'}}), self.now))
        report = self.run_export()
        self.assertEqual(report['issues'][0]['state'], 'closed_without_retry')
        self.assertEqual(report['attempts']['old']['state'], 'interrupted')

    def test_review_completion_is_bound_to_snapshot_and_latest_does_not_regress(self):
        import hashlib
        self.run_export('2026-11-14')
        snapshot = self.out / '2026-11-14.json'
        checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        note = self.out / 'review.md'
        note.write_text('Checked retained attempts; missing historical dates remain unknown.')
        with self.assertRaises(ValueError):
            mark_reviewed(self.out, '2026-11-14', note, 'stale')
        mark_reviewed(self.out, '2026-11-14', note, checksum)
        self.run_export('2026-11-13')
        state = json.loads((self.out / 'collection.json').read_text())
        latest = json.loads((self.out / 'latest.json').read_text())
        self.assertEqual(state['last_completed_review_date'], '2026-11-14')
        self.assertEqual(state['reviews']['2026-11-14']['snapshot_sha256'], checksum)
        self.assertEqual(latest['date'], '2026-11-14')
        self.assertTrue((self.out / 'snapshots' / (checksum + '.json')).exists())
