import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class PublicationReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.settings = Settings(self.root / "home", self.root, self.root, Path('/usr/bin/python3'),
                                 notifications=False, checked_publication=True)
        self.bridge = Bridge(self.settings, object())
        self.addCleanup(self.bridge.store.close)
        self.artifact = self.settings.home / 'learning/artifacts/v1'
        self.artifact.mkdir(parents=True)
        body = '---\nname: portable\nlearning_scope: global_candidate\n---\nInspect sources before describing behavior.\n'
        (self.artifact / 'SKILL.md').write_text(body)
        self.value = {'name': 'portable', 'version': 'v1', 'content': body, 'resource_dir': str(self.artifact),
                      'files': {'SKILL.md': hashlib.sha256(body.encode()).hexdigest()}}
        self.context = {'version': 'catalog-v1', 'memories': {}, 'skills': [{'name': 'portable', 'version': 'c1'}],
                        'bridge_provenance': {'skills': {'portable': {'projects': [str(self.root)]}}}}
        self.bridge.store.set('knowledge-index-v1', self.context['bridge_provenance'])
        self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})
        self.review = {'name': 'portable', 'version': 'v1', 'files': self.value['files'],
                       'sources': [str(self.root)], 'decision': 'approve_generic',
                       'note': 'Reviewed every file; methods are reusable without project-private content.',
                       'evidence': 'local-audit:publication-review'}

    def submit(self, **kw):
        return self.bridge.rpc('review_publication', {'cwd': str(self.root), 'review': self.review, **kw})

    def test_review_is_previewable_exact_version_and_published_by_owner(self):
        self.assertEqual(self.submit()['state'], 'preview')
        self.assertIsNone(self.bridge.store.get('publication-reviews-v1'))
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            self.bridge.publish_checked(self.context)
            self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'data_classification_unassessed')
            self.assertEqual(self.submit(dry_run=False)['state'], 'queued')
            self.bridge.publish_checked(self.context)
        self.assertEqual(self.bridge.store.get('publications-v1')['portable']['artifact']['version'], 'v1')
        changed = {**self.value, 'version': 'v2'}
        context = {**self.context, 'version': 'catalog-v2'}
        with patch('hermes_codex_learning.native_reads.skill', return_value=changed):
            self.bridge.publish_checked(context)
        self.assertEqual(self.bridge.store.get('publications-v1')['portable']['artifact']['version'], 'v1')
        self.assertFalse(self.bridge.store.get('publication-decisions')['portable']['allowed'])

    def test_stale_manifest_or_source_cannot_be_approved(self):
        for change in ({'version': 'old'}, {'files': {}}, {'sources': ['/foreign']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.bridge.rpc('review_publication', {'cwd': str(self.root), 'review': {**self.review, **change}, 'dry_run': False})
        self.assertIsNone(self.bridge.store.get('publication-reviews-v1'))

    def test_review_does_not_bypass_data_policy_executable_or_failed_host_gates(self):
        self.bridge.settings = replace(self.settings, data_policy={'enabled': True})
        with self.assertRaisesRegex(ValueError, 'assessment policy'):
            self.submit(dry_run=False)
        self.bridge.settings = self.settings
        script = self.artifact / 'example.py'
        script.write_text('print(1)')
        self.value['files']['example.py'] = hashlib.sha256(script.read_bytes()).hexdigest()
        self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})
        with self.assertRaisesRegex(ValueError, 'executable_resource'):
            self.submit(dry_run=False)
        del self.value['files']['example.py']
        self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})
        self.submit(dry_run=False)
        with self.bridge.store.db:
            self.bridge.store.db.execute('INSERT INTO withheld_versions VALUES (?,?,?)', ('portable', 'v1', '{}'))
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            self.bridge.publish_checked(self.context)
        self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'host_validation_failed')
        self.assertEqual(self.bridge.store.get('publications-v1'), {})
