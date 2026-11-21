import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.knowledge import context_view
from hermes_codex_learning.notices import NoticeQueue


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
        self.context = {'version': 'catalog-v1', 'memories': {}, 'skills': [{'name': 'portable', 'version': 'c1', 'artifact_version': 'v1'}],
                        'bridge_provenance': {'skills': {'portable': {'projects': [str(self.root)]}}}}
        self.bridge.store.set('knowledge-index-v1', self.context['bridge_provenance'])
        self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})
        self.review = {'name': 'portable', 'version': 'v1', 'files': self.value['files'],
                       'sources': [str(self.root)], 'decision': 'approve_generic',
                       'note': 'Reviewed every file; methods are reusable without project-private content.',
                       'evidence': 'local-audit:publication-review'}

    def submit(self, **kw):
        return self.bridge.rpc('review_publication', {'cwd': str(self.root), 'review': self.review, **kw})

    def review_action(self, action, *, cwd=None, **params):
        return self.bridge.rpc('publication_review', {'cwd': str(cwd or self.root), 'action': action, **params})

    def publish(self):
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            return self.bridge.publish_checked(self.context)

    def enable_automatic_review(self, mode):
        self.bridge.settings = replace(self.settings, capture_scope='global',
                                       codex_skills_dir=self.root / 'codex-skills')
        self.bridge.rpc('publication_policy', {'mode': mode})
        quiet_period = patch('hermes_codex_learning.publication_guardian.QUIET_SECONDS', 0)
        quiet_period.start()
        self.addCleanup(quiet_period.stop)
        self.addCleanup(self.bridge.publication_guardian.close)

    def finish_review(self):
        _, _, future = self.bridge.publication_guardian.inflight
        future.result(timeout=5)
        return self.publish()

    def approval_notice_count(self):
        return self.bridge.store.db.execute(
            "SELECT count(*) FROM notices WHERE json_extract(payload,'$.approval_subject')='publication'"
        ).fetchone()[0]

    def load_version(self, cwd, version):
        return self.bridge.rpc('skill_load', {'cwd': str(cwd), 'name': 'portable', 'artifact_version': version})

    def add_resources(self, version, **resources):
        directory = self.settings.home / 'learning/artifacts' / version
        directory.mkdir()
        for name, content in resources.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.value = {**self.value, 'version': version, 'resource_dir': str(directory), 'content': resources['SKILL.md'],
                      'files': {name: hashlib.sha256(body.encode()).hexdigest() for name, body in resources.items()}}
        self.context['version'] = version
        self.context['skills'][0]['artifact_version'] = version
        self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})

    def test_existing_candidate_gets_one_actionable_notice_and_scoped_file_preview(self):
        self.add_resources('v2', **{'SKILL.md': self.value['content'], 'references/checks.md': 'Reusable checks.'})
        self.bridge.store.set('publication-context-version', 'pre-review build')
        self.bridge.store.set('publication-decisions', {'portable': {'allowed': False, 'version': 'v2',
                                                                  'reason': 'data_classification_unassessed'}})
        self.publish()
        self.context['version'] = 'next-context-version'
        self.publish()
        candidates = self.review_action('list')['candidates']
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual((candidate['name'], candidate['version'], candidate['sources']),
                         ('portable', 'v2', [str(self.root)]))
        self.assertEqual([(r['path'], r['change']) for r in candidate['resources']],
                         [('SKILL.md', 'added'), ('references/checks.md', 'added')])
        params = {'name': 'portable', 'revision': candidate['revision'], 'file': 'references/checks.md'}
        self.assertEqual(self.review_action('file', **params), {'before': None, 'after': 'Reusable checks.'})
        foreign = self.root.parent / 'another-project'
        self.bridge.settings = replace(self.settings, capture_scope='global')
        self.assertEqual(self.review_action('list', cwd=foreign), {'candidates': [], 'decisions': []})
        with self.assertRaises(ValueError):
            self.review_action('file', cwd=foreign, **params)
        with self.assertRaises(ValueError):
            self.review_action('file', **{**params, 'file': '../../private.txt'})
        queue = NoticeQueue(self.settings.state)
        self.addCleanup(queue.close)
        notice, = queue.sync('test')['items']
        self.assertEqual((notice['kind'], notice['approval_subject'], notice['scope']),
                         ('approval.required', 'publication', str(self.root)))
        self.assertTrue(notice['project_approval'])
        self.assertIn('通用技能待审', notice['summary'])
        self.assertNotIn('Reusable checks.', json.dumps(notice))
        self.assertIsNone(self.bridge.store.get('publication-reviews-v1'))

    def test_keep_new_revision_preserves_last_shared_copy_and_new_changes_return_to_review(self):
        self.add_resources('v2', **{'SKILL.md': self.value['content'], 'references/old.md': 'Old reference'})
        self.publish()
        candidate, = self.review_action('list')['candidates']
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            result = self.review_action('approve_generic', name='portable', revision=candidate['revision'])
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(self.review_action('list')['decisions'][0]['state'], 'queued')
        self.assertEqual(self.bridge.store.get('publications-v1'), {})
        self.publish()
        self.assertEqual(self.review_action('list')['decisions'][0]['state'], 'published')
        previous_body = self.value['content']
        self.add_resources('v3', **{'SKILL.md': previous_body + 'Add a check.\n', 'references/new.md': 'New reference'})
        self.publish()
        candidate, = self.review_action('list')['candidates']
        self.assertEqual(candidate['previous_version'], 'v2')
        self.assertEqual({r['path']: r['change'] for r in candidate['resources']},
                         {'SKILL.md': 'changed', 'references/new.md': 'added', 'references/old.md': 'removed'})
        params = {'name': 'portable', 'revision': candidate['revision']}
        self.assertEqual(self.review_action('file', **params, file='SKILL.md'),
                         {'before': previous_body, 'after': self.value['content']})
        self.assertEqual(self.review_action('file', **params, file='references/old.md'),
                         {'before': 'Old reference', 'after': None})
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            self.assertEqual(self.review_action('keep_project', **params)['state'], 'kept_in_project')
        checked = self.publish()
        self.assertEqual(checked['publications']['portable']['version'], 'v2')
        self.assertEqual(context_view(checked, str(self.root))['skills'][0]['artifact_version'], 'v3')
        self.assertEqual(context_view(checked, '/unrelated', 'portable')['skills'][0]['artifact_version'], 'v2')
        self.assertEqual(self.review_action('list')['candidates'], [])
        self.assertEqual(self.review_action('list')['decisions'][0]['state'], 'kept_in_project')
        with self.assertRaises(ValueError):
            self.review_action('approve_generic', **params)
        self.add_resources('v4', **{'SKILL.md': previous_body + 'Different change.\n'})
        self.publish()
        self.assertEqual(self.review_action('list')['candidates'][0]['version'], 'v4')

    def test_fresh_native_version_hashes_and_origin_are_required_for_a_decision(self):
        self.publish()
        candidate, = self.review_action('list')['candidates']
        params = {'name': 'portable', 'revision': candidate['revision']}
        with patch('hermes_codex_learning.native_reads.skill', return_value={**self.value, 'version': 'new'}):
            with self.assertRaises(ValueError):
                self.review_action('approve_generic', **params)
        body = self.artifact / 'SKILL.md'
        body.write_text('Changed after displaying review')
        with self.assertRaises(ValueError):
            self.review_action('file', **params, file='SKILL.md')
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            with self.assertRaisesRegex(ValueError, 'resource_changed'):
                self.review_action('approve_generic', **params)
        self.bridge.store.set('knowledge-index-v1', {'skills': {'portable': {'projects': [str(self.root), '/another']}}})
        with self.assertRaises(ValueError):
            self.review_action('keep_project', **params)
        self.assertIsNone(self.bridge.store.get('publication-reviews-v1'))

    def test_other_automatic_blocks_do_not_offer_publication_approval(self):
        frontmatter = self.value['content'].split('Inspect sources')[0]
        for settings, extra, reason in (
            (replace(self.settings, data_policy={'enabled': True}), 'No assessment command.', 'data_classification_unassessed'),
            (self.settings, 'Project source ' + str(self.root), 'project_private_material'),
            (replace(self.settings, quality_checks=True), 'No required sections.', 'missing_applicability_limits_or_verification'),
        ):
            with self.subTest(reason=reason):
                self.bridge.settings = settings
                body = self.artifact / 'SKILL.md'
                content = frontmatter + extra
                body.write_text(content)
                self.value = {**self.value, 'content': content, 'files': {'SKILL.md': hashlib.sha256(content.encode()).hexdigest()}}
                self.bridge.store.set('artifact-read-cache', {'portable': {'artifact': self.value}})
                self.context['version'] = extra
                self.publish()
                self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], reason)
                self.assertEqual(self.review_action('list')['candidates'], [])
        self.assertFalse(self.bridge.store.db.execute(
            "SELECT 1 FROM notices WHERE json_extract(payload,'$.approval_subject')='publication'").fetchone())

    def test_unassessed_executable_needs_an_independent_proof_before_review(self):
        self.bridge.settings = replace(self.settings, quality_checks=True)
        self.add_resources('v2', **{'SKILL.md': self.value['content'], 'scripts/example.py': 'print(1)\n'})
        with patch('hermes_codex_learning.quality.verify') as verify:
            self.publish()
        verify.assert_not_called()
        self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'],
                         'executable_resource_needs_validation')
        self.assertEqual(self.review_action('list')['candidates'], [])

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

    def test_autonomous_first_publication_can_be_withdrawn_from_other_projects(self):
        self.enable_automatic_review('autonomous')
        other = self.root.parent / (self.root.name + '-reader')
        verdict = {'verdict': 'APPROVE', 'reason': 'generic_guidance', 'model': 'fixture'}
        with patch('hermes_codex_learning.publication_guardian.run_guardian', return_value=verdict) as reviewer:
            self.assertNotIn('portable', self.publish()['publications'])
            with self.assertRaises(PermissionError):
                self.load_version(other, 'v1')
            view = self.finish_review()
            self.bridge.cache_context(view, self.bridge.store)
        reviewer.assert_called_once()
        request = reviewer.call_args.args[1]
        self.assertEqual((request['mode'], request['previous'], request['baseline']), ('autonomous', {}, {}))
        self.assertEqual(request['candidate'], self.value['files'])
        self.assertEqual(self.approval_notice_count(), 0)
        self.assertEqual(self.load_version(other, 'v1')['content'], self.value['content'])
        projected = self.bridge.settings.codex_skills_dir / 'portable' / 'SKILL.md'
        self.assertEqual(projected.read_text(), self.value['content'])
        target, = self.review_action('list')['rollbacks']
        self.assertIsNone(target['previous_version'])
        proof = self.bridge.store.get('publication-guardian:' + target['automatic_review'])
        self.assertEqual(proof['candidate']['files'], self.value['files'])
        self.assertNotIn('documents', proof)
        action = {'name': 'portable', 'revision': target['revision']}
        with self.assertRaises(ValueError):
            self.review_action('rollback', cwd=other, **action)
        self.assertEqual(self.review_action('rollback', **action)['state'], 'rolled_back')
        self.assertEqual(self.bridge.rpc('context', {'cwd': str(other), 'query': 'portable'})['skills'], [])
        self.assertFalse(projected.parent.is_symlink())
        with self.assertRaises(PermissionError):
            self.load_version(other, 'v1')
        self.assertEqual(self.load_version(self.root, 'v1')['content'], self.value['content'])
        self.assertNotIn('portable', self.publish()['publications'])
        self.assertEqual(self.review_action('list')['candidates'], [])

    def test_smart_update_restores_the_human_approved_version_across_projects(self):
        self.submit(dry_run=False)
        self.publish()
        self.enable_automatic_review('smart')
        original = self.value['content']
        self.add_resources('v2', **{'SKILL.md': original + 'Check the current sources.\n'})
        other = self.root.parent / (self.root.name + '-reader')
        verdict = {'verdict': 'APPROVE', 'reason': 'same_scope', 'model': 'fixture'}
        with patch('hermes_codex_learning.publication_guardian.run_guardian', return_value=verdict) as reviewer:
            self.assertEqual(self.publish()['publications']['portable']['version'], 'v1')
            self.assertEqual(self.load_version(other, 'v1')['content'], original)
            view = self.finish_review()
            self.bridge.cache_context(view, self.bridge.store)
        reviewer.assert_called_once()
        target, = self.review_action('list')['rollbacks']
        proof = self.bridge.store.get('publication-guardian:' + target['automatic_review'])
        self.assertEqual((proof['previous_version'], proof['baseline_version']), ('v1', 'v1'))
        self.assertEqual(self.approval_notice_count(), 0)
        self.assertEqual(self.load_version(other, 'v2')['content'], self.value['content'])
        projected = self.bridge.settings.codex_skills_dir / 'portable' / 'SKILL.md'
        self.assertEqual(projected.read_text(), self.value['content'])
        self.review_action('rollback', name='portable', revision=target['revision'])
        recalled = self.bridge.rpc('context', {'cwd': str(other), 'query': 'portable'})
        self.assertEqual(recalled['skills'][0]['artifact_version'], 'v1')
        self.assertEqual(self.load_version(other, 'v1')['content'], original)
        self.assertEqual(projected.read_text(), original)
        with self.assertRaises(ValueError):
            self.load_version(other, 'v2')
        self.assertEqual(self.load_version(self.root, 'v2')['content'], self.value['content'])
        self.assertEqual(self.publish()['publications']['portable']['version'], 'v1')

    def test_autonomous_denial_is_quiet_and_escalation_shows_the_latest_diff(self):
        self.submit(dry_run=False)
        self.publish()
        self.enable_automatic_review('autonomous')
        original = self.value['content']
        versions = (
            ('v2', 'DENY', 'project_specific'),
            ('v3', 'ESCALATE', 'permissions_changed'),
            ('v4', 'ESCALATE', 'review_unavailable'),
        )
        previous_revision = None
        for version, verdict, reason in versions:
            with self.subTest(version=version):
                self.add_resources(version, **{'SKILL.md': original + 'Changed check ' + version})
                with patch('hermes_codex_learning.publication_guardian.run_guardian',
                           return_value={'verdict': verdict, 'reason': reason}):
                    self.publish()
                    self.assertEqual(self.finish_review()['publications']['portable']['version'], 'v1')
                listing = self.review_action('list')
                if verdict == 'DENY':
                    self.assertEqual(self.approval_notice_count(), 0)
                    self.assertEqual(listing['candidates'], [])
                    self.assertEqual(listing['decisions'][0]['state'], 'automatic_kept')
                    continue
                self.assertEqual(self.approval_notice_count(), 1)
                candidate, = listing['candidates']
                self.assertEqual(candidate['version'], version)
                self.assertTrue(candidate['reason'])
                diff = self.review_action('file', name='portable', revision=candidate['revision'], file='SKILL.md')
                self.assertEqual(diff, {'before': original, 'after': self.value['content']})
                if previous_revision:
                    with self.assertRaises(ValueError):
                        self.review_action('approve_generic', name='portable', revision=previous_revision)
                previous_revision = candidate['revision']

    def test_superseded_version_and_prior_policy_generation_cannot_publish(self):
        self.submit(dry_run=False)
        self.publish()
        self.enable_automatic_review('smart')
        original = self.value['content']
        release = threading.Event()
        self.addCleanup(release.set)

        def review(settings, request):
            if not release.wait(5):
                raise TimeoutError('Test did not release the pending review')
            return {'verdict': 'APPROVE', 'reason': 'same_scope'}

        with patch('hermes_codex_learning.publication_guardian.run_guardian', side_effect=review) as reviewer:
            self.add_resources('v2', **{'SKILL.md': original + 'Clarification A.'})
            self.publish()
            self.add_resources('v3', **{'SKILL.md': original + 'Clarification B.'})
            self.publish()
            release.set()
            self.assertEqual(self.finish_review()['publications']['portable']['version'], 'v1')
            self.bridge.rpc('publication_policy', {'mode': 'manual'})
            self.assertEqual(self.finish_review()['publications']['portable']['version'], 'v1')
            self.assertEqual(self.review_action('list')['candidates'][0]['version'], 'v3')
            self.bridge.rpc('publication_policy', {'mode': 'smart'})
            self.publish()
            self.assertEqual(self.finish_review()['publications']['portable']['version'], 'v3')
        self.assertEqual(reviewer.call_count, 3)

    def test_autonomous_review_cannot_bypass_hard_blocks_or_operator_keep(self):
        self.publish()
        candidate, = self.review_action('list')['candidates']
        with patch('hermes_codex_learning.native_reads.skill', return_value=self.value):
            self.review_action('keep_project', name='portable', revision=candidate['revision'])
        self.enable_automatic_review('autonomous')
        with patch('hermes_codex_learning.publication_guardian.run_guardian') as reviewer:
            self.assertNotIn('portable', self.publish()['publications'])
            self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'operator_kept_in_project')
            original = self.value['content']
            self.add_resources('v2', **{'SKILL.md': original + str(self.root)})
            self.assertNotIn('portable', self.publish()['publications'])
            self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'project_private_material')
            self.add_resources('v3', **{'SKILL.md': original, 'scripts/run.py': 'print(1)\n'})
            self.assertNotIn('portable', self.publish()['publications'])
            self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'executable_resource_needs_validation')
            self.add_resources('v4', **{'SKILL.md': original + 'Clarification.'})
            with self.bridge.store.db:
                self.bridge.store.db.execute('INSERT INTO withheld_versions VALUES (?,?,?)', ('portable', 'v4', '{}'))
            self.assertNotIn('portable', self.publish()['publications'])
            self.assertEqual(self.bridge.store.get('publication-decisions')['portable']['reason'], 'host_validation_failed')
            self.assertEqual(self.review_action('list')['candidates'], [])
            self.assertIsNone(self.bridge.publication_guardian.inflight)
            reviewer.assert_not_called()

    def test_trusted_local_allows_internal_urls_and_local_paths_while_blocking_credentials(self):
        from hermes_codex_learning import publication_review
        self.bridge.settings = replace(self.settings, data_policy={'enabled': True, 'mode': 'trusted_local'})
        body = (
            '---\nname: portable\n---\n'
            'Check https://cloud.bytedance.net/docs and /data00/home/example/project before applying.\n'
        )
        self.add_resources('v3', **{'SKILL.md': body})
        publication_review.verify_candidate(self.bridge.settings, self.bridge.store, 'portable', self.value, [str(self.root)])

        leaked = '---\nname: portable\n---\nAuthorization: ' + 'Bearer ' + 'eyJhbGciOiJIUzI1NiJ9.aaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbb\n'
        self.add_resources('v4', **{'SKILL.md': leaked})
        with self.assertRaisesRegex(ValueError, 'credential_material'):
            publication_review.verify_candidate(self.bridge.settings, self.bridge.store, 'portable', self.value, [str(self.root)])
