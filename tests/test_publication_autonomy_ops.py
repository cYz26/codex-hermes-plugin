import argparse
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning import operations, publication_review
from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import Store


class PublicationAutonomyOperationsTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = str(root)
        self.settings = Settings(root / 'home', root, root, Path('/usr/bin/python3'), checked_publication=True)
        self.store = Store(self.settings.state)
        self.addCleanup(self.store.close)

    def published(self, version, **check):
        root = self.settings.home / 'learning/artifacts' / version
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_bytes(version.encode())
        artifact = {'name': 'portable', 'version': version, 'resource_dir': str(root),
                    'files': {'SKILL.md': hashlib.sha256(version.encode()).hexdigest()}}
        return {'artifact': artifact, 'sources': [self.project], 'check': {'allowed': True, **check}}

    def install(self, previous):
        current = self.published('v2', automatic_review={'id': 'guardian-run', 'previous_version': previous})
        self.store.set('publications-v1', {'portable': current})
        self.store.set('publication-decisions', {'portable': {'allowed': True, 'version': 'v2'}})
        self.store.set('publication-aliases', {'old-name': 'portable', 'unrelated': 'other'})
        if previous:
            self.store.set('publication-history:portable', {previous: self.published(previous)})
        return publication_review.list_pending(self.settings, self.store, self.project)['rollbacks'][0]['revision']

    def undo(self, revision, project=None):
        return publication_review.rollback(self.settings, self.store, project or self.project, 'portable', revision)

    def test_first_publication_can_be_withdrawn_only_by_exact_project_and_version(self):
        revision = self.install(None)
        with self.assertRaises(ValueError):
            self.undo(revision, '/another-project')
        with self.assertRaises(ValueError):
            self.undo('obsolete')
        result = self.undo(revision)
        self.assertIsNone(result['previous_version'])
        self.assertEqual(self.store.get('publications-v1'), {})
        self.assertEqual(self.store.get('publication-aliases'), {'unrelated': 'other'})
        self.assertEqual(self.store.get('publication-decisions')['portable']['reason'], publication_review.ROLLED_BACK)
        self.assertEqual(self.store.get(publication_review.ROLLBACKS)['portable']['v2'], result)
        with self.assertRaises(ValueError):
            self.undo(revision)
        self.assertTrue((self.settings.home / 'learning/artifacts/v2/SKILL.md').is_file())

    def test_existing_smart_review_record_can_restore_only_intact_previous_snapshot(self):
        revision = self.install('v1')
        previous_file = self.settings.home / 'learning/artifacts/v1/SKILL.md'
        previous_file.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '快照'):
            self.undo(revision)
        self.assertEqual(self.store.get('publications-v1')['portable']['artifact']['version'], 'v2')
        self.assertIsNone(self.store.get(publication_review.ROLLBACKS))
        previous_file.write_bytes(b'v1')
        self.undo(revision)
        self.assertEqual(self.store.get('publications-v1')['portable']['artifact']['version'], 'v1')
        scoped = publication_review.list_pending(self.settings, self.store, self.project)
        self.assertEqual(scoped['restored'][0]['version'], 'v2')
        self.assertNotIn('rollbacks', scoped)

    def test_policy_cli_queries_or_changes_service_mode(self):
        parser = argparse.ArgumentParser()
        operations.add_parsers(parser.add_subparsers(dest='command'))
        with patch.object(Settings, 'load', return_value=self.settings), patch('hermes_codex_learning.ipc.request') as request:
            for mode in [None, 'manual', 'smart', 'autonomous']:
                args = parser.parse_args(['publication-policy', *([mode] if mode else [])])
                operations.execute(args)
                request.assert_called_with(self.settings, 'publication_policy', {'mode': mode})
