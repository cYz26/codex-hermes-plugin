import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from hermes_codex_learning.review_resolution import submit, process
from hermes_codex_learning.store import Store


class ReviewResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)); self.addCleanup(self.store.close)
        self.request = {'request_id': 'request', 'state': 'unknown', 'project': '/project',
                        'handle': 'scope', 'intent_id': 'intent', 'results': [
                            {'id': 'attempt', 'artifact_versions': {'skill': 'v1'}}]}
        self.store.db.execute('INSERT INTO learning_requests VALUES (?,?,?,?,?,?)',
                             ('request', '/project', 'chat', json.dumps(self.request), 0, 0))
        self.store.db.commit()
        self.store.set('artifact-read-cache', {'skill': {'artifact': {'version': 'v1'}}})
        self.inspection = {'request_id': 'request', 'attempt_id': 'attempt', 'outcome_hash': 'native-hash',
                           'artifact_versions': {'skill': 'v1'}, 'note': 'Checked full current artifacts',
                           'evidence': 'audit:exact-inspection'}

    def test_preview_is_readonly_and_only_serial_owner_applies_the_inspection(self):
        preview = submit(self.store, '/project', self.inspection)
        key = 'review-inspection:' + preview['inspection_id']
        self.assertEqual(preview['state'], 'preview')
        self.assertIsNone(self.store.get(key))
        queued = submit(self.store, '/project', self.inspection, dry_run=False)
        self.assertEqual(queued['state'], 'queued')
        api = types.SimpleNamespace(close_review_without_retry=Mock(return_value={'state': 'closed_without_retry'}))
        bridge = types.SimpleNamespace(store=self.store, api=api)
        process(bridge); process(bridge)
        api.close_review_without_retry.assert_called_once()
        self.assertEqual(self.store.get(key)['state'], 'closed_without_retry')
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload FROM learning_requests').fetchone()[0])['state'], 'unknown')

    def test_foreign_scope_stale_artifact_and_native_rejection_remain_blocked(self):
        with self.assertRaises(ValueError):
            submit(self.store, '/foreign', self.inspection, dry_run=False)
        with self.assertRaises(ValueError):
            submit(self.store, '/project', {**self.inspection, 'artifact_versions': {'skill': 'old'}})
        queued = submit(self.store, '/project', self.inspection, dry_run=False)
        bridge = types.SimpleNamespace(store=self.store, api=types.SimpleNamespace(
            close_review_without_retry=Mock(side_effect=ValueError('native artifacts changed'))))
        process(bridge)
        self.assertEqual(self.store.get('review-inspection:' + queued['inspection_id'])['state'], 'rejected')
