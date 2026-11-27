import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.store import Store, digest, dumps
from hermes_codex_learning.config import Settings


class RetentionTests(unittest.TestCase):
    def test_processed_body_is_removed_but_same_event_cannot_replay(self):
        from hermes_codex_learning.retention import run
        from hermes_codex_learning.normalize import merge_event
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/'home', root, root, Path('/usr/bin/python3'))
            store = Store(settings.state)
            event = {"hook_event_name":"UserPromptSubmit","cwd":directory,"session_id":"c","turn_id":"t","prompt":"BODY-TO-EXPIRE"}
            try:
                event_id = store.capture(event)
                row = store.db.execute("SELECT rowid,* FROM events WHERE id=?", (event_id,)).fetchone()
                position = row["rowid"]
                data = merge_event(store,row)
                data.update(status="completed")
                old = time.time()-40*86400
                store.db.execute("UPDATE turns SET payload=?,observed_revision=revision,activity=?", (json.dumps(data),old))
                store.db.execute("UPDATE events SET delivered=?,created=?", (old,old))
                store.db.commit()
                source = {k:data["source"][k] for k in ("host","project","chat")}
                store.set("chat-handles:"+digest(source), ["handle"])
                proof = {"review_handle":"handle", "learning_evidence":{"state":"passed",
                         "claims":[{"basis":[{"ref":"T1.request", "quote":"BODY-TO-EXPIRE"}]}]}}
                store.set("semantic-skill-attestations", {"active":proof})
                store.set("archived-skill-origins", {"archived":{"attestation":proof}})
                api = SimpleNamespace(prune=lambda **kw: {"dry_run":kw["dry_run"]},
                                      read=lambda **kw: {"retired":True})
                preview = run(settings,store,api,dry_run=True)
                self.assertEqual(preview["eligible_turns"],1)
                self.assertIn("BODY-TO-EXPIRE", store.db.execute("SELECT payload FROM events").fetchone()[0])
                done = run(settings,store,api,dry_run=False)
                self.assertEqual(done["retired_turns"],1)
                self.assertEqual(store.capture(event),event_id)
                self.assertEqual(store.pending(),[])
                with closing(Store(settings.state)) as reopened:
                    next_event = reopened.capture({**event, "turn_id": "next"})
                    row = reopened.db.execute("SELECT rowid FROM events WHERE id=?", (next_event,)).fetchone()
                    self.assertGreater(row[0], position)
                    reopened.set("recovery-cursor", position + 10)
                    last_event = reopened.capture({**event, "turn_id": "last"})
                    row = reopened.db.execute("SELECT rowid FROM events WHERE id=?", (last_event,)).fetchone()
                    self.assertEqual(row[0], position + 11)
                self.assertNotIn("BODY-TO-EXPIRE",store.db.execute("SELECT payload FROM turns").fetchone()[0])
                for key in ("semantic-skill-attestations", "archived-skill-origins"):
                    self.assertNotIn("BODY-TO-EXPIRE", json.dumps(store.get(key)))
                    self.assertIn("retired_source_quotes", json.dumps(store.get(key)))
            finally:
                store.close()

    def test_batches_pass_old_unretired_turns_and_preserve_pending_evidence(self):
        from hermes_codex_learning.retention import run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / 'home', root, root, Path('/usr/bin/python3'))
            with closing(Store(settings.state)) as store:
                old = time.time() - 40 * 86400
                handles = ['busy', 'busy', 'retired', 'retired', 'retired', 'retired']
                for turn, handle in enumerate(handles):
                    chat = f'chat-{handle}'
                    source = {'host': 'fixture', 'project': directory, 'chat': chat}
                    data = {'source': {**source, 'turn': str(turn)}, 'status': 'completed',
                            'user': 'fixture request', 'coverage': ['background_exit_pending'] if turn == 5 else []}
                    identity = digest([chat, str(turn)])
                    event = store.capture({'hook_event_name': 'Stop', 'session_id': chat, 'turn_id': str(turn)})
                    with store.db:
                        store.db.execute('INSERT INTO turns VALUES (?,?,?,?,?,?)',
                                         (identity, dumps(data), 'r', 'r', old + turn, old + turn))
                        if turn != 4:
                            store.db.execute('UPDATE events SET delivered=? WHERE id=?', (old, event))
                    store.set('chat-handles:' + digest(source), [handle])

                reads = []
                def state(*, kind, name):
                    reads.append(name)
                    return {'retired': name == 'retired'}
                api = SimpleNamespace(prune=lambda **kw: {}, read=state)
                with patch('hermes_codex_learning.retention.BATCH_SIZE', 2):
                    first = run(settings, store, api, dry_run=False)
                    self.assertTrue(first['more_raw_candidates'])
                    self.assertTrue(first['more_turn_candidates'])
                    self.assertEqual((first['pruned_raw_events'], first['retired_turns']), (2, 0))
                    self.assertEqual(reads, ['busy'])
                    cursor = store.get('retention:turn-cursor')
                    preview = run(settings, store, api)
                    self.assertEqual((preview['eligible_raw_events'], preview['eligible_turns']), (2, 2))
                    self.assertEqual(store.get('retention:turn-cursor'), cursor)
                    second = run(settings, store, api, dry_run=False)
                    self.assertFalse(second['more_raw_candidates'])
                    self.assertFalse(second['more_turn_candidates'])
                    self.assertEqual((second['pruned_raw_events'], second['retired_turns']), (2, 2))
                    self.assertIsNone(store.get('retention:turn-cursor'))
                kept = {json.loads(row[0])['turn_id'] for row in store.db.execute('SELECT payload FROM events')}
                self.assertEqual(kept, {'4', '5'})

    def test_reclaims_reusable_pages_on_new_database_and_reports_legacy_layout(self):
        from hermes_codex_learning.retention import run

        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = Settings(root / 'home', root, root, Path('/usr/bin/python3'))
                if legacy:
                    settings.state.mkdir(parents=True)
                    with closing(sqlite3.connect(settings.state / 'bridge.sqlite')) as db:
                        db.execute('PRAGMA auto_vacuum=NONE')
                        db.execute('CREATE TABLE existing (value TEXT)')
                with closing(Store(settings.state)) as store:
                    store.set('fixture', 'x' * 512_000)
                    with store.db:
                        store.db.execute("DELETE FROM meta WHERE key='fixture'")
                    api = SimpleNamespace(prune=lambda **kw: {})
                    preview = run(settings, store, api)['database']
                    applied = run(settings, store, api, dry_run=False)['database']
                    self.assertGreater(preview['reusable_bytes'], 0)
                    self.assertEqual(applied['incremental_vacuum'], not legacy)
                    if legacy:
                        self.assertEqual(applied, preview)
                    else:
                        self.assertLess(applied['allocated_bytes'], preview['allocated_bytes'])
                        self.assertLess(applied['reusable_bytes'], preview['reusable_bytes'])
