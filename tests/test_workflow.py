import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.store import Store
from hermes_codex_learning.workflow import project_event, observation


class WorkflowTests(unittest.TestCase):
    def test_async_test_poll_is_joined_without_retaining_arguments(self):
        from hermes_codex_learning.normalize import merge_event
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                events = [
                    {"hook_event_name":"PostToolUse","tool_use_id":"start","tool_name":"exec_command",
                     "tool_input":{"cmd":"python -m pytest PRIVATE"},"tool_response":{"session_id":17}},
                    {"hook_event_name":"PostToolUse","tool_use_id":"poll","tool_name":"write_stdin",
                     "tool_input":{"session_id":17},"tool_response":{"exit_code":0}},
                    {"hook_event_name":"Stop"},
                ]
                for event in events:
                    value = project_event(dict(event,cwd=directory,session_id="chat",turn_id="turn"))
                    identity = store.capture(value)
                    data = merge_event(store, store.db.execute("SELECT * FROM events WHERE id=?", (identity,)).fetchone())
                self.assertEqual(len(data["tools"]), 1)
                self.assertEqual(data["tools"]["start"]["name"], "workflow_test")
                self.assertEqual(data["tools"]["start"]["status"], "completed")
                self.assertNotIn("PRIVATE", json.dumps(data))
            finally:
                store.close()
    def test_arbitrary_output_and_tool_names_cannot_enter_the_projection(self):
        projected = project_event({'hook_event_name': 'PostToolUse', 'tool_name': 'PRIVATE_TOOL',
                                   'tool_input': {'secret': 'PRIVATE'}, 'tool_response': 'PRIVATE exit_code=0'})
        self.assertEqual(projected['tool_name'], 'workflow_other')
        self.assertEqual(projected['tool_response'], {'outcome': 'unknown'})
        self.assertNotIn('PRIVATE', json.dumps(projected))

    def test_unknown_outcome_is_not_reinterpreted_as_a_success(self):
        data = {'source': {'host': 'h', 'project': '/p', 'chat': 'c', 'turn': 't'}, 'status': 'completed',
                'tools': {'x': {'name': 'workflow_test', 'status': 'unknown'}}}
        value = observation(data)
        self.assertIn('unknown', value['user'])
        self.assertNotIn('completed', value['user'])

    def test_unassessed_turn_is_projected_before_storage_and_native_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / 'home', root, root, Path('/usr/bin/python3'),
                                data_policy={'enabled': True, 'workflow_projection': True})
            events = [
                {'hook_event_name': 'UserPromptSubmit', 'prompt': 'PRIVATE-PROMPT'},
                {'hook_event_name': 'PreToolUse', 'tool_use_id': 't1', 'tool_name': 'Bash',
                 'tool_input': {'command': 'python -m pytest PRIVATE-FILE'}},
                {'hook_event_name': 'PostToolUse', 'tool_use_id': 't1', 'tool_name': 'Bash',
                 'tool_input': {'command': 'python -m pytest PRIVATE-FILE'},
                 'tool_response': {'exit_code': 1, 'output': 'PRIVATE-OUTPUT'}},
                {'hook_event_name': 'Stop', 'last_assistant_message': 'PRIVATE-ANSWER'},
            ]
            for event in events:
                hook({**event, 'session_id': 'private-chat', 'turn_id': 'private-turn', 'cwd': directory}, settings)
            observed = []
            class API:
                def open(self, source):
                    return {'handle': 'workflow-handle'}
                def observe(self, handle, value, **kwargs):
                    observed.append(value)
            bridge = Bridge(settings, API())
            try:
                bridge.ingest()
                persisted = str(bridge.store.db.execute('SELECT payload FROM events').fetchall())
                self.assertNotIn('PRIVATE-', persisted)
                self.assertEqual(len(observed), 1)
                outbound = json.dumps(observed)
                for private in ('PRIVATE-', directory, 'private-chat', 'private-turn'):
                    self.assertNotIn(private, outbound)
                self.assertIn('test', outbound)
                self.assertIn('failed', outbound)
                self.assertFalse(observed[0]['tools'])
            finally:
                bridge.store.close()

    def test_explicit_disable_keeps_unassessed_body_out_of_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root, root, Path('/usr/bin/python3'),
                                data_policy={'enabled': True, 'workflow_projection': False})
            hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'c', 'turn_id': 't',
                  'cwd': directory, 'prompt': 'PRIVATE'}, settings)
            store = Store(settings.state)
            try:
                self.assertEqual(store.pending(), [])
            finally:
                store.close()

    def test_workflow_name_and_origin_do_not_admit_unattested_or_later_edited_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root, root, Path('/usr/bin/python3'), checked_publication=True,
                                data_policy={'enabled': True, 'workflow_projection': True})
            name = 'workflow-test-checks'
            body = '---\nname: workflow-test-checks\nlearning_scope: global_candidate\n---\nVerify outcomes.'
            values = {}
            for version in ('v1', 'v2'):
                path = root / 'learning/artifacts' / version / 'SKILL.md'
                path.parent.mkdir(parents=True)
                path.write_text(body + version)
                values[version] = {'name': name, 'version': version, 'content': path.read_text(),
                                   'resource_dir': str(path.parent),
                                   'files': {'SKILL.md': hashlib.sha256(path.read_bytes()).hexdigest()}}
            class API:
                selected = 'v1'
                def read(self, **kwargs):
                    return values[self.selected]
            api = API()
            bridge = Bridge(settings, api)
            context = {'version': 'catalog1', 'skills': [{'name': name, 'version': 'catalog1'}],
                       'bridge_provenance': {'skills': {name: {'projects': ['/codex-workflow']}}}}
            try:
                self.assertFalse(bridge.publish_checked(context)['publications'])
                bridge.store.set('workflow-skill-attestations', {name: {'version': 'v1', 'catalog_version': 'catalog1'}})
                self.assertEqual(bridge.publish_checked(context)['publications'][name]['version'], 'v1')
                api.selected = 'v2'
                context['version'] = 'catalog2'
                # Curator or any other writer cannot inherit the prior source attestation.
                self.assertEqual(bridge.publish_checked(context)['publications'][name]['version'], 'v1')
                self.assertFalse(bridge.store.get('publication-decisions')[name]['allowed'])
            finally:
                bridge.store.close()
