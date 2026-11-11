import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.notices import NoticeQueue
from test_desktop_receiver import receiver


class NoticeSyncTests(unittest.TestCase):
    def test_unknown_first_page_does_not_block_later_receipts_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = NoticeQueue(root/'server')
            config = dict(state=str(root/'desktop'), host='fixture', bridge='/bridge',
                          settings='/settings', notifier='/notifier')
            client = receiver.Receiver(config)
            def remote(action, *args, body=None):
                if action == 'capabilities':
                    return {'protocol_version': 2}
                if action == 'sync':
                    return queue.sync(client.consumer, after=body['after'], acknowledgements=body['acks'])
                self.fail('Unexpected legacy operation: ' + action)
            def add(identifier):
                queue.publish(dict(id=identifier, kind='update.notice', message='Synthetic notice', receipt=identifier))
            try:
                for i in range(20):
                    add(str(i))
                with patch.object(client, 'remote', side_effect=remote), \
                     patch.object(receiver.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as post:
                    client.poll()  # Accepted marker missing: outcome unknown.
                    add('new-21')
                    client.poll()
                    self.assertEqual(post.call_count, 2)
                client.db.close()
                client = receiver.Receiver(config)
                with patch.object(client, 'remote', side_effect=remote), \
                     patch.object(receiver.subprocess, 'run') as post:
                    client.poll()
                    post.assert_not_called()
                self.assertEqual(client.db.execute('SELECT COUNT(*) FROM received').fetchone()[0], 21)
            finally:
                client.db.close()
                queue.close()

    def test_batch_acknowledgements_are_scope_checked_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = NoticeQueue(Path(directory))
            try:
                queue.publish(dict(id='n', kind='update.notice', message='fixture', receipt='fixture'))
                first = queue.sync('desktop', after=0)
                self.assertEqual(first['items'][0]['text'], 'fixture')
                bad = [{'id':'n', 'scope':'wrong', 'stage':'sent'}]
                with self.assertRaises(PermissionError):
                    queue.sync('desktop', after=first['cursor'], acknowledgements=bad)
                good = [{'id':'n', 'scope':'system', 'stage':'read'}]
                queue.sync('desktop', after=first['cursor'], acknowledgements=good)
                queue.sync('desktop', after=first['cursor'], acknowledgements=good)
                self.assertEqual(queue.poll('desktop'), [])
                self.assertEqual(queue.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 2)
            finally:
                queue.close()
