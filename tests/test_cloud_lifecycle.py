import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.store import atomic_json, digest
from hermes_codex_learning.supervisor import Supervisor
from hermes_codex_learning.systemd import systemd
import test_updates as fixtures


class CloudLifecycleTests(unittest.TestCase):
    def test_site_publication_failure_does_not_leave_committed_worker_paused(self):
        fixture = fixtures.ActivationTests()
        fixture.setUp()
        try:
            fixture.before['updates'] = {'site_manifest': str(fixture.root / 'site/migration.json')}
            atomic_json(fixture.config, fixture.before)
            fixture.candidate['site'] = {'fixture': True}
            atomic_json(fixture.updater.directory / 'candidate.json', fixture.candidate)
            worker = fixtures.FakeWorker(fixture.home)
            supervisor = Supervisor(fixture.config, worker=worker)
            with patch('hermes_codex_learning.site_updates.publish_binding', side_effect=OSError('fixture failure')):
                supervisor.activate()
            self.assertTrue(worker.resumed)
            transaction = json.loads((fixture.updater.directory / 'transaction.json').read_text())
            self.assertEqual(transaction['state'], 'committed')
            self.assertEqual(transaction['site_publication'], 'pending')
            with patch('hermes_codex_learning.site_updates.publish_binding'):
                supervisor.finish_site_publication()
            transaction = json.loads((fixture.updater.directory / 'transaction.json').read_text())
            self.assertEqual(transaction['site_publication'], 'complete')
        finally:
            fixture.tearDown()

    def test_active_service_replacement_drains_restarts_and_checks_new_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'bridge.json'
            atomic_json(config, dict(home=str(root/'learning'), project=str(root),
                                    source=str(root/'native'), python=sys.executable))
            unit = root / '.config/systemd/user/hermes-learning-pilot.service'
            unit.parent.mkdir(parents=True)
            unit.write_text('[Service]\nExecStart=/old/service\n')
            commands = []
            def run(argv, **kwargs):
                commands.append(argv)
                return SimpleNamespace(returncode=0, stdout='active\n')
            with patch('hermes_codex_learning.systemd.Path.home', return_value=root), \
                 patch('hermes_codex_learning.systemd.subprocess.run', side_effect=run), \
                 patch('hermes_codex_learning.systemd.drain_service', create=True) as drain, \
                 patch('hermes_codex_learning.systemd.verify_service', create=True) as verify, \
                 contextlib.redirect_stdout(io.StringIO()):
                systemd(SimpleNamespace(settings=str(config), start=True, replace=True, dry_run=False))
            drain.assert_called_once()
            verify.assert_called_once()
            self.assertIn(['systemctl', '--user', 'restart', unit.name], commands)

    def test_workflow_delivery_and_session_close_keep_the_actual_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = Settings(root/'home', root, root, Path(sys.executable), capture_scope='global',
                                data_policy={'enabled': True, 'workflow_projection': True})
            class API:
                def __init__(self):
                    self.sources, self.observed, self.deliveries = {}, [], []
                def open(self, source):
                    handle = digest(source)
                    self.sources[handle] = source
                    return {'handle': handle}
                def observe(self, handle, value, **kwargs):
                    self.observed.append(handle)
                def record_usage(self, token, evidence):
                    self.deliveries.append((token, evidence))
                    return {'recorded': True}
            api = API()
            bridge = Bridge(settings, api)
            events = [
                dict(hook_event_name='UserPromptSubmit', prompt='PRIVATE-TASK'),
                dict(hook_event_name='PostToolUse', tool_use_id='load', tool_name='mcp__hermes_learning__skill_load',
                     tool_response={'structuredContent': {'name':'workflow-example','delivery_token':'fixture-token',
                                                          'content':'PRIVATE-SKILL'}}),
                dict(hook_event_name='Stop', last_assistant_message='PRIVATE-ANSWER'),
                dict(hook_event_name='SessionEnd'),
            ]
            try:
                for event in events:
                    hook(dict(event, session_id='chat', turn_id='turn', cwd=directory), settings)
                bridge.ingest()
                self.assertEqual(len(api.deliveries), 1)
                self.assertEqual(set(bridge.store.get('closing').values()), set(api.observed))
                payloads = ''.join(row[0] for row in bridge.store.db.execute('SELECT payload FROM events'))
                self.assertNotIn('PRIVATE-', payloads)
                source = bridge.store.get('workflow-local-source:' + api.observed[0])
                self.assertEqual(source['project'], str(root))
            finally:
                bridge.store.close()
