import tempfile
import unittest
import time
import json
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class LifecycleTests(unittest.TestCase):
    def test_synchronous_native_maintenance_reports_its_worker_phase(self):
        from hermes_codex_learning.lifecycle import maintenance
        from hermes_codex_learning.progress import summarize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api = SimpleNamespace()
            bridge = Bridge(Settings(root/'home', root, root, Path('/usr/bin/python3')), api)
            self.addCleanup(bridge.store.close)
            bridge.store.set('loop-progress', {'phase':'reviewing', 'updated_at':time.time()})
            def native(**kwargs):
                self.assertEqual(summarize(bridge.store)['worker_position']['phase'], 'maintaining')
                return {'curator':None}
            api.maintenance = native
            self.assertEqual(maintenance(bridge), {'curator':None})

    def bridge(self, root, events):
        api = SimpleNamespace(read=lambda **kwargs: {
            'events': events, 'cursor': events[-1]['id'] if events else 'baseline',
            'cursor_found': True, 'has_more': False})
        bridge = Bridge(Settings(root/'home', root/'project', root, Path('/usr/bin/python3')), api)
        bridge.native_capabilities['skill_lifecycle'] = True
        bridge.store.set('native-lifecycle-cursor', 'baseline')
        self.addCleanup(bridge.store.close)
        return bridge

    def test_native_merge_keeps_both_origins_and_old_name_routes_after_publication(self):
        from hermes_codex_learning.lifecycle import synchronize, aliases_for
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [
                {'id': 'one', 'action': 'delete', 'skill': 'learned-a', 'absorbed_into': 'learned-combined'},
                {'id': 'two', 'action': 'delete', 'skill': 'learned-b', 'absorbed_into': 'learned-combined'}])
            bridge.store.set('knowledge-index-v1', {'memory': {}, 'skills': {
                'learned-a': {'projects': ['/a']}, 'learned-b': {'projects': ['/b']}}})
            synchronize(bridge)
            index = bridge.store.get('knowledge-index-v1')
            self.assertEqual(index['skills']['learned-combined']['projects'], ['/a', '/b'])
            self.assertEqual(aliases_for(bridge.store, {}), {})
            self.assertEqual(aliases_for(bridge.store, {'learned-combined': {}}),
                             {'learned-a': 'learned-combined', 'learned-b': 'learned-combined'})
            self.assertEqual(bridge.store.get('native-lifecycle-cursor'), 'two')

    def test_unknown_source_does_not_borrow_the_known_targets_origin(self):
        from hermes_codex_learning.lifecycle import synchronize
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [
                {'id': 'one', 'action': 'delete', 'skill': 'unknown', 'absorbed_into': 'known'}])
            bridge.store.set('knowledge-index-v1', {'skills': {'known': {'projects': ['/a']}}})
            synchronize(bridge)
            self.assertEqual(bridge.store.get('lifecycle-withheld')['known']['reason'], 'unknown_merge_source')

    def test_restoration_recovers_original_scope_and_removes_forwarding(self):
        from hermes_codex_learning.lifecycle import synchronize
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [
                {'id': 'one', 'action': 'delete', 'skill': 'original', 'absorbed_into': 'combined'},
                {'id': 'two', 'action': 'restore', 'skill': 'original'}])
            bridge.store.set('knowledge-index-v1', {'skills': {'original': {'projects': ['/a']}}})
            synchronize(bridge)
            self.assertEqual(bridge.store.get('knowledge-index-v1')['skills']['original']['projects'], ['/a'])
            self.assertNotIn('original', bridge.store.get('native-skill-aliases'))

    def test_missing_native_cursor_exposes_gap_without_guessing_lineage(self):
        from hermes_codex_learning.lifecycle import synchronize
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            bridge.api.read = lambda **kw: {'events': [], 'cursor': 'new', 'cursor_found': False, 'has_more': False}
            synchronize(bridge)
            self.assertEqual(bridge.store.get('native-lifecycle-status')['state'], 'history_gap')

    def test_merge_preserves_enterprise_scope_without_reusing_validation(self):
        from hermes_codex_learning.lifecycle import synchronize
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [
                {'id': 'one', 'action': 'delete', 'skill': 'old', 'absorbed_into': 'combined'}])
            bridge.store.set('knowledge-index-v1', {'skills': {'old': {'projects': ['/a']}}})
            bridge.store.set('semantic-skill-attestations', {'old': {'boundary_id': 'enterprise', 'version': 'old-proof'}})
            synchronize(bridge)
            proof = bridge.store.get('semantic-skill-attestations')['combined']
            self.assertEqual(proof['boundary_id'], 'enterprise')
            self.assertNotIn('version', proof)

    def test_native_related_skill_is_recalled_but_unrelated_own_skill_is_not_recommended(self):
        from hermes_codex_learning.knowledge import context_view
        for forward in (False, True):
            with self.subTest(forward=forward):
                view = context_view({'memories': {}, 'skills': [
                    {'name': 'csv', 'description': 'CSV ingestion',
                     'related_skills': ['atomic-output', 'private-output'] if forward else []},
                    {'name': 'atomic-output', 'related_skills': [] if forward else ['csv']},
                    {'name': 'private-output', 'related_skills': ['csv']},
                    {'name': 'notifications', 'description': 'notification senders'}],
                    'checked_publication': True,
                    'bridge_provenance': {'skills': {
                        'csv': {'projects': ['/a']}, 'atomic-output': {'projects': ['/a']},
                        'notifications': {'projects': ['/a']}, 'private-output': {'projects': ['/b']}}}},
                    '/a', 'CSV')
                self.assertEqual(view['recommended_skills'], ['csv', 'atomic-output'])

    def test_retention_protects_published_rollback_and_recently_retired_versions(self):
        from hermes_codex_learning.retention import retain_artifacts
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            versions = [str(i)*64 for i in range(1, 8)]
            def record(v):
                return {'artifact': {'version': v}, 'retired_at': time.time()-40*86400}
            bridge.store.set('publications-v1', {'skill': record(versions[0])})
            bridge.store.set('publication-history:skill', {v: record(v) for v in versions[1:4]})
            bridge.store.set('artifact-retired-at', {versions[4]: time.time()})
            bridge.store.set('artifact-read-cache', {'private': record(versions[5])})
            seen = []
            bridge.api.prune_artifacts = lambda protected, **kw: seen.extend(protected) or {'removed': []}
            retain_artifacts(bridge.store, bridge.api)
            self.assertEqual(set(seen), {versions[0], versions[2], versions[3], versions[4], versions[5]})
            bridge.store.set('native-candidate-intents', {'ancestor': {
                'version': versions[1], 'sources': {'original': versions[6]}}, 'candidate': {
                'version': versions[3], 'sources': {'ancestor': versions[1]}}})
            seen.clear()
            retain_artifacts(bridge.store, bridge.api)
            self.assertEqual(set(seen), set(versions))

    def test_ended_incomplete_evidence_does_not_keep_the_host_busy_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            row = {'activity': time.time(), 'payload': json.dumps({'status': 'incomplete',
                    '_ended_by': 'Stop', 'tools': {'opaque': {'status': 'unknown'}}})}
            bridge.latest_turns = lambda: [row]
            self.assertFalse(bridge.has_active_turns())
            row['payload'] = json.dumps({'status': 'running'})
            self.assertTrue(bridge.has_active_turns())

    def test_configuration_recovers_only_the_same_authorized_enterprise_change(self):
        from hermes_codex_learning.lifecycle import configure
        from hermes_codex_learning.enterprise import make_boundary, boundary
        from hermes_codex_learning.store import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            config = bridge.settings.home/'config.yaml'
            config.write_text('model: approved-route\n')
            record = bridge.settings.state/'enterprise.json'
            atomic_json(record, make_boundary(bridge.settings))
            bridge.settings = replace(bridge.settings, data_policy={
                'mode': 'trusted_enterprise', 'enterprise_boundary': str(record)})
            original_id = boundary(bridge.settings.data_policy)['id']
            bridge.api.read = lambda **kw: {'enabled': True, 'consolidate': False, 'configuration_scope_hash': 'same-model'}
            calls = []
            def change():
                calls.append(True)
                config.write_text('model: approved-route\ncurator: {enabled: true, consolidate: true}\n')
                if len(calls) == 1:
                    raise RuntimeError('simulated interruption')
                return {'enabled': True, 'consolidate': True}
            bridge.api.configure_curator = change
            bridge.store.set('native-curator-config-request', {'at': time.time()})
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                configure(bridge)
            self.assertIsNone(boundary(bridge.settings.data_policy))
            configure(bridge)
            self.assertEqual(boundary(bridge.settings.data_policy)['id'], original_id)
            self.assertEqual(bridge.store.get('native-curator-config-result')['state'], 'applied')
            record.unlink()
            bridge.store.set('native-curator-config-request', {'at': time.time()})
            configure(bridge)
            self.assertEqual(len(calls), 2)
            self.assertEqual(bridge.store.get('native-curator-config-result')['state'], 'refused')

    def test_curator_scope_preserves_native_writes_without_reading_foreign_skills(self):
        from hermes_codex_learning.lifecycle import curation_tool_guard, _CURATION_SCOPE
        token = _CURATION_SCOPE.set({'owned': {'approved'}, 'present': {'approved', 'foreign'}})
        try:
            self.assertEqual(curation_tool_guard(tool_name='skill_view', args={'name': 'foreign'})['action'], 'block')
            self.assertEqual(curation_tool_guard(tool_name='skill_manage', args={'action': 'create', 'name': 'foreign'})['action'], 'block')
            self.assertIsNone(curation_tool_guard(tool_name='skill_view', args={'name': 'approved'}))
            self.assertIsNone(curation_tool_guard(tool_name='skill_manage', args={'action': 'create', 'name': 'combined'}))
            self.assertIsNone(curation_tool_guard(tool_name='skill_manage', args={
                'action': 'delete', 'name': 'approved', 'absorbed_into': 'combined'}))
            self.assertEqual(curation_tool_guard(tool_name='skills_list', args={})['action'], 'block')
            _CURATION_SCOPE.get()['present'].remove('foreign')
            self.assertIsNone(curation_tool_guard(tool_name='skills_list', args={}))
        finally:
            _CURATION_SCOPE.reset(token)

    def test_redacted_admission_cannot_expose_the_original_native_package(self):
        from hermes_codex_learning.lifecycle import curation_admission
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self.bridge(root, [])
            bridge.settings = replace(bridge.settings, data_policy={'enabled': True})
            bridge.store.set('knowledge-index-v1', {'skills': {'approved': {'projects': ['/a']}}})
            (root/'SKILL.md').write_text('sensitive fixture body')
            bridge.api.read = lambda **kw: {'resource_dir': str(root), 'files': {'SKILL.md': 'fixture'}}
            with patch('hermes_codex_learning.policy.admit', return_value={'allowed': True, 'payload': {'resources': {}}}):
                self.assertEqual(curation_admission(bridge, ['approved']), 'curator_resource_requires_redaction')

    def test_native_revision_without_archiving_inherits_admitted_input_scope(self):
        from hermes_codex_learning.lifecycle import inherit_curator_inputs
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            bridge.store.set('knowledge-index-v1', {'skills': {
                'a': {'projects': ['/a']}, 'b': {'projects': ['/b']}}})
            bridge.api.read = lambda **kw: {'skills': [
                {'name': 'a', 'version': 'revised'}, {'name': 'b', 'version': 'same'},
                {'name': 'new', 'version': 'new-version'}]}
            inherit_curator_inputs(bridge, {'before': {'a': 'old', 'b': 'same'}, 'inputs': {'a', 'b'}})
            index = bridge.store.get('knowledge-index-v1')['skills']
            self.assertEqual(index['a']['projects'], ['/a', '/b'])
            self.assertEqual(index['new']['projects'], ['/a', '/b'])
            self.assertEqual(index['b']['projects'], ['/b'])

    def test_interrupted_curator_does_not_publish_unconfirmed_mutations(self):
        from hermes_codex_learning.lifecycle import reconcile_interrupted_curation
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.bridge(Path(directory), [])
            bridge.store.set('native-curator-checkpoint', {'before': {'old': 'v1'}, 'at': 1})
            reconcile_interrupted_curation(bridge, {'skills': [{'name': 'old', 'version': 'v2'}]})
            self.assertEqual(bridge.store.get('lifecycle-withheld')['old']['reason'], 'native_curator_result_unconfirmed')
            self.assertIsNone(bridge.store.get('native-curator-checkpoint'))

    def test_native_candidate_intent_cannot_override_explicit_project_scope(self):
        from hermes_codex_learning.policy import sharing_check
        value = {'content': '---\nname: local\n learning_scope: project\n---\nPrivate context.'}
        self.assertEqual(sharing_check(value, ['/a'], [], native_candidate=True)['reason'], 'project_experience')

    def test_failed_activation_restores_native_lineage_with_knowledge(self):
        from hermes_codex_learning.supervisor import snapshot, restore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self.bridge(root, [])
            bridge.store.set('native-skill-aliases', {'old': 'combined'})
            bridge.store.set('native-lifecycle-cursor', 'before')
            snapshot(bridge.settings, root/'snapshot')
            bridge.store.set('native-skill-aliases', {'old': 'invalid'})
            bridge.store.set('native-lifecycle-cursor', 'after')
            restore(bridge.settings, root/'snapshot')
            self.assertEqual(bridge.store.get('native-skill-aliases'), {'old': 'combined'})
            self.assertEqual(bridge.store.get('native-lifecycle-cursor'), 'before')
