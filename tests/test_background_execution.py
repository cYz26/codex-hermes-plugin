import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.progress import summarize
from hermes_codex_learning.store import digest


FIXTURE = Path(__file__).with_name('fixtures') / 'codex-background-80469.json'
CALL = 'exec-launch-80469'


class BackgroundExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.transcript = self.sessions / 'turn.jsonl'
        self.enterContext(patch.dict(os.environ, {'CODEX_HOME': str(self.root)}))
        self.fixture = json.loads(FIXTURE.read_text())
        self.chat, self.turn = self.fixture['chat'], self.fixture['turn']
        self.settings = Settings(self.root / 'hermes', self.root, self.root, Path(sys.executable), notifications=False)
        self.observations = {}

        def observe(handle, body, *, event_id):
            if event_id in self.observations:
                previous, receipt = self.observations[event_id]
                if previous != body:
                    raise ValueError('Observation identity conflict')
                return receipt
            receipt = {'handle': handle, 'revision': len(self.observations) + 1}
            self.observations[event_id] = (body, receipt)
            return receipt

        self.api = SimpleNamespace(open=lambda source: {'handle': source['chat']}, observe=observe)
        self.bridge = Bridge(self.settings, self.api)
        self.addCleanup(self.bridge.store.close)

    def write_transcript(self, rows):
        header = {'type': 'session_meta', 'payload': {'id': self.chat, 'cwd': str(self.root),
                  'cli_version': self.fixture['cli_version']}}
        self.transcript.write_text(''.join(json.dumps(row) + '\n' for row in [header, *rows]))

    def capture(self, event, *, at=None):
        event_id = self.bridge.store.capture({
            'session_id': self.chat, 'turn_id': self.turn, 'cwd': str(self.root),
            'bridge_host': 'fixture-host', 'bridge_project': str(self.root),
            'transcript_path': str(self.transcript), **event,
        })
        if at is not None:
            with self.bridge.store.db:
                self.bridge.store.db.execute('UPDATE events SET created=? WHERE id=?', (at, event_id))
        return event_id

    def stage_original_turn(self):
        self.write_transcript(self.fixture['transcript'])
        pre, stop = self.fixture['hooks']
        self.capture({'hook_event_name': 'UserPromptSubmit', 'prompt': 'Prepare a preview.'}, at=pre['created'] - 1)
        self.capture(pre['payload'], at=pre['created'])
        return self.capture(stop['payload'], at=stop['created'])

    def progress(self):
        return summarize(self.bridge.store, project=str(self.root), chat=self.chat, turn=self.turn)['turns'][0]

    def append_exit(self, exit_code):
        row = {'type': 'event_msg', 'ordinal': 653, 'timestamp': '2026-11-14T01:53:00Z',
               'payload': {'type': 'item_completed', 'turn_id': self.turn,
                           'item': {'type': 'CommandExecution', 'id': CALL, 'status': 'completed' if exit_code == 0 else 'failed',
                                    'exit_code': exit_code}}}
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(row) + '\n')

    def test_original_stop_admits_turn_with_background_session_still_running(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        progress = self.progress()
        self.assertEqual(progress['execution']['turn_status'], 'completed')
        self.assertEqual(progress['admission']['state'], 'admitted')
        self.assertIn('background_exit_pending', progress['coverage']['gaps'])
        self.assertEqual(set(self.observations), {stop})
        body, _ = self.observations[stop]
        self.assertEqual(body['status'], 'completed')
        tool, = body['tools']
        self.assertEqual((tool['call_id'], tool['status'], tool['process']), (CALL, 'running', '80469'))
        self.assertIsNone(tool['result'])
        self.assertEqual(progress['execution']['correctness'], 'not_inferred')
        self.assertEqual(progress['execution']['pending_processes'], [{
            'tool_call': CALL, 'session_id': '80469', 'status': 'running', 'observed_at': '2026-11-14T01:43:54.896Z',
        }])

    def test_recovery_reuses_first_stop_body_even_if_transcript_gains_exit_evidence(self):
        stop = self.stage_original_turn()
        observe = self.api.observe

        def lost_reply(handle, body, *, event_id):
            observe(handle, body, event_id=event_id)
            raise ConnectionError('Reply lost after native commit')

        with patch.object(self.api, 'observe', lost_reply), self.assertRaises(ConnectionError):
            self.bridge.ingest()
        before = self.observations[stop]
        self.append_exit(7)
        self.bridge.store.close()
        self.bridge = Bridge(self.settings, self.api)
        self.addCleanup(self.bridge.store.close)
        self.bridge.ingest()
        self.assertEqual(self.observations, {stop: before})
        self.assertEqual(self.progress()['last_position']['native_revision'], 1)
        self.assertEqual(self.progress()['consumption']['pending_events'], 0)
        self.assertIn('background_exit_pending', self.progress()['coverage']['gaps'])

    def test_late_exact_exit_updates_original_call_once_and_preserves_failure(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        self.append_exit(7)
        post = {**self.fixture['hooks'][0]['payload'], 'hook_event_name': 'PostToolUse',
                'tool_response': 'Unstructured shell output; the host transcript has the exit code.'}
        first = self.capture(post)
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, first})
        tool, = self.observations[first][0]['tools']
        self.assertEqual((tool['call_id'], tool['status'], tool['result']['exit_code']), (CALL, 'failed', 7))
        self.assertNotIn('background_exit_pending', self.progress()['coverage']['gaps'])
        self.capture({**post, 'tool_response': 'Duplicate delivery with different output.'})
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, first})
        self.assertEqual(self.progress()['last_position']['native_revision'], 2)

    def test_opaque_late_result_does_not_resolve_or_duplicate_background_call(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        post = {**self.fixture['hooks'][0]['payload'], 'hook_event_name': 'PostToolUse',
                'tool_response': '{"exit_code":0,"output":"a command can print this"}'}
        self.capture(post)
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop})
        self.assertEqual(self.progress()['execution']['turn_status'], 'completed')
        self.assertIn('background_exit_pending', self.progress()['coverage']['gaps'])

    def test_post_before_host_exit_is_rechecked_using_its_own_event_id(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        post = self.capture({**self.fixture['hooks'][0]['payload'], 'hook_event_name': 'PostToolUse',
                             'tool_response': 'Output received before the exit code was persisted.'})
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop})
        self.append_exit(0)
        with patch('hermes_codex_learning.daemon.time.time', return_value=4_000_000_000):
            self.bridge.retry_completed_inputs()
            self.bridge.retry_completed_inputs()
        self.assertEqual(set(self.observations), {stop, post})
        self.assertEqual(self.observations[post][0]['tools'][0]['result']['exit_code'], 0)

    def test_terminal_post_is_recoverable_after_merge_but_before_native_observe(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        post = self.capture({**self.fixture['hooks'][0]['payload'], 'hook_event_name': 'PostToolUse',
                             'tool_response': {'exit_code': 7}})
        with patch.object(self.bridge, 'observe_turn', side_effect=ConnectionError('Interrupted before native submission')):
            with self.assertRaises(ConnectionError):
                self.bridge.ingest()
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, post})
        self.assertEqual(self.observations[post][0]['tools'][0]['status'], 'failed')

    def test_poll_exit_is_recoverable_after_merge_but_before_native_observe(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        poll = self.capture({'hook_event_name': 'PostToolUse', 'tool_name': 'write_stdin', 'tool_use_id': 'poll',
                             'tool_input': {'session_id': 80469}, 'tool_response': {'exit_code': 0}})
        with patch.object(self.bridge, 'observe_turn', side_effect=ConnectionError('Interrupted before native submission')):
            with self.assertRaises(ConnectionError):
                self.bridge.ingest()
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, poll})
        self.assertEqual(self.observations[poll][0]['tools'][0]['call_id'], CALL)

    def test_old_transcript_cursor_is_reread_and_unknown_siblings_keep_the_gate_closed(self):
        from hermes_codex_learning.host_evidence import read_turn

        self.stage_original_turn()
        cursor = read_turn(self.transcript, self.chat, self.turn, str(self.root), include_task=False)['cursor']
        cursor.pop('version', None)
        cursor.pop('code_execs', None)
        self.bridge.store.set('transcript-cursor:' + digest([self.chat, self.turn, False]), cursor)
        self.capture({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'unmatched',
                      'tool_input': {'command': 'unknown command'}}, at=self.fixture['hooks'][0]['created'] + 1)
        self.bridge.ingest()
        self.assertIn('background_exit_pending', self.progress()['coverage']['gaps'])
        self.assertEqual(self.progress()['execution']['turn_status'], 'incomplete')
        self.assertEqual(self.progress()['admission']['state'], 'awaiting_terminal_evidence')
        self.assertEqual(self.observations, {})

    def test_repeated_stop_does_not_replace_the_original_receipt(self):
        _, terminal = self.fixture['hooks']
        stop = self.stage_original_turn()
        self.bridge.ingest()
        self.assertEqual(self.capture(terminal['payload']), stop)
        self.capture({**terminal['payload'], 'last_assistant_message': 'Changed duplicate Stop.'})
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop})
        self.assertEqual(self.progress()['last_position']['native_revision'], 1)

    def test_another_terminal_tool_can_be_observed_while_the_background_exit_is_pending(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        other = self.capture({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'tool_use_id': 'other',
                              'tool_input': {'command': 'fixture'}, 'tool_response': {'exit_code': 0}})
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, other})
        self.assertIn('background_exit_pending', self.progress()['coverage']['gaps'])

    def test_repeated_poll_of_the_settled_session_does_not_add_a_tool(self):
        stop = self.stage_original_turn()
        self.bridge.ingest()
        poll = {'hook_event_name': 'PostToolUse', 'tool_name': 'write_stdin', 'tool_use_id': 'poll',
                'tool_input': {'session_id': 80469}, 'tool_response': {'exit_code': 0}}
        exit_event = self.capture(poll)
        self.bridge.ingest()
        tool, = self.observations[exit_event][0]['tools']
        self.assertEqual((tool['call_id'], tool['status']), (CALL, 'completed'))
        self.capture({**poll, 'hook_event_name': 'PreToolUse', 'tool_use_id': 'later-poll', 'tool_response': None})
        self.capture({**poll, 'tool_use_id': 'later-poll'})
        self.bridge.ingest()
        self.assertEqual(set(self.observations), {stop, exit_event})
        self.assertEqual(self.progress()['execution']['turn_status'], 'completed')
        self.assertEqual(self.progress()['last_position']['native_revision'], 2)

    def test_unverified_or_ambiguous_launch_does_not_bypass_unknown_result_protection(self):
        variants = ('forged_script', 'foreign_turn', 'client_authored', 'duplicate_outer', 'duplicate_pre',
                    'foreign_pre', 'bad_session', 'no_start', 'no_stop')
        for variant in variants:
            with self.subTest(variant=variant):
                self.chat, self.turn = 'chat-' + variant, 'turn-' + variant
                call, response, _ = deepcopy(self.fixture['transcript'])
                for row in (call, response):
                    row['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = self.turn
                rows = [call, response]
                if variant == 'forged_script':
                    call['payload']['input'] = 'text({"session_id":80469});'
                elif variant == 'foreign_turn':
                    response['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'different-turn'
                elif variant == 'client_authored':
                    response['metadata']['client_authored'] = True
                elif variant == 'duplicate_outer':
                    rows.insert(1, {**call, 'payload': {**call['payload'], 'call_id': 'other'}})
                elif variant == 'bad_session':
                    response['payload']['output'][1]['text'] = response['payload']['output'][1]['text'].replace('80469', 'true')
                elif variant == 'no_start':
                    rows = [response]
                self.write_transcript(rows)
                pre, stop = self.fixture['hooks']
                self.capture({'hook_event_name': 'UserPromptSubmit', 'prompt': 'Prepare a preview.'}, at=pre['created'] - 1)
                pre_event = dict(pre['payload'])
                if variant == 'foreign_pre':
                    pre_event['bridge_host'] = 'different-host'
                self.capture(pre_event, at=pre['created'])
                if variant == 'foreign_pre':
                    self.capture({**pre['payload'], 'tool_input': {'command': 'different command'}}, at=pre['created'] + 0.1)
                if variant == 'duplicate_pre':
                    self.capture({**pre_event, 'tool_use_id': 'other-exec'}, at=pre['created'] + 0.1)
                if variant != 'no_stop':
                    self.capture(stop['payload'], at=stop['created'])
                self.bridge.ingest()
                expected = 'running' if variant == 'no_stop' else 'incomplete'
                self.assertEqual(self.progress()['execution']['turn_status'], expected)
                self.assertNotIn('background_exit_pending', self.progress()['coverage']['gaps'])
        self.assertEqual(self.observations, {})

    @unittest.skipUnless(os.environ.get('HERMES_LEARNING_NATIVE_SOURCE'), 'Requires the pinned Hermes Learning API source')
    def test_native_journal_counts_turn_once_and_only_counts_real_exit(self):
        env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'TMPDIR', 'HERMES_LEARNING_NATIVE_SOURCE'}}
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '-v',
                                 'BackgroundExecutionTests._verify_native_journal'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _verify_native_journal(self):
        # Exercise the actual Learning API and native counters. Stub only the
        # provider boundary; the fixture must never run a model or a shell.
        native = Path(os.environ['HERMES_LEARNING_NATIVE_SOURCE']).resolve()
        sys.path.insert(0, str(native))
        self.addCleanup(sys.path.remove, str(native))
        self.enterContext(patch.dict(os.environ, {'HERMES_HOME': str(self.settings.home)}))
        from agent.learning_api import LearningFacade
        from agent.learning_runtime import NativeLearningRuntime
        from hermes_codex_learning.evidence import organize_review_history

        runtime_agent = SimpleNamespace(_memory_nudge_interval=1000, _skill_nudge_interval=1000,
                                        valid_tool_names={'memory', 'skill_manage'}, _memory_store=True)
        self.enterContext(patch.object(NativeLearningRuntime, 'agent', return_value=runtime_agent))
        self.enterContext(patch.object(NativeLearningRuntime, 'project', return_value='not_configured'))
        model = self.enterContext(patch.object(NativeLearningRuntime, 'review', side_effect=AssertionError('Model not allowed')))
        api = LearningFacade(self.settings.home, 'background-execution-test')
        self.addCleanup(api.close)
        self.bridge.api = api
        stop = self.stage_original_turn()
        self.capture({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'tool_use_id': 'other-terminal',
                      'tool_input': {'command': 'fixture'}, 'tool_response': {'exit_code': 0}},
                     at=self.fixture['hooks'][0]['created'] + 1)
        self.bridge.ingest()

        def journal(event_id):
            db = api._service.db
            row = db.execute('SELECT handle,body,messages FROM observations WHERE id=?', (event_id,)).fetchone()
            receipt = json.loads(db.execute('SELECT payload FROM receipts WHERE id=?', (event_id,)).fetchone()[0])
            return row['handle'], json.loads(row['body']), json.loads(row['messages']), receipt

        handle, body, messages, initial = journal(stop)
        self.assertEqual((initial['new_user_turn'], initial['completed_tool_delta']), (True, 1))
        self.assertEqual((runtime_agent._turns_since_memory, runtime_agent._iters_since_skill), (1, 1))
        self.assertEqual(len([message for message in messages if message['role'] == 'tool']), 1)
        self.assertEqual(api.observe(handle, body, event_id=stop), initial)
        with self.assertRaisesRegex(ValueError, 'Observation identity conflict'):
            api.observe(handle, {**body, 'coverage': []}, event_id=stop)
        history, report = organize_review_history(api, handle, body['source'], messages,
                                                  {'revision': initial['revision']}, settings=self.settings)
        self.assertIn({'turn': self.turn, 'reason': 'background_exit_pending'}, report['coverage_gaps'])
        self.assertIn('"reason":"background_exit_pending"', history[0]['content'])

        self.append_exit(7)
        post_payload = {**self.fixture['hooks'][0]['payload'], 'hook_event_name': 'PostToolUse',
                        'tool_response': 'See the matching host exit.'}
        post = self.capture(post_payload)
        self.bridge.ingest()
        _, exit_body, exit_messages, final = journal(post)
        self.assertEqual((final['new_user_turn'], final['completed_tool_delta']), (False, 1))
        self.assertEqual((runtime_agent._turns_since_memory, runtime_agent._iters_since_skill), (1, 2))
        self.assertEqual([tool['result']['exit_code'] for tool in exit_body['tools'] if tool['call_id'] == CALL], [7])
        self.assertEqual(self.capture(post_payload), post)
        self.capture({**post_payload, 'tool_response': 'Repeated host receipt.'})
        self.bridge.ingest()
        self.assertEqual(api.read(kind='status', detail='summary')['counts']['observations'], 2)
        history, report = organize_review_history(api, handle, body['source'], messages + exit_messages,
                                                  {'revision': final['revision']}, settings=self.settings)
        self.assertNotIn({'turn': self.turn, 'reason': 'background_exit_pending'}, report['coverage_gaps'])
        model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
