import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.host_evidence import read_turn


class IncrementalHostTests(unittest.TestCase):
    def test_changed_file_boundary_cannot_reuse_old_terminal_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'session.jsonl'
            meta={'type':'session_meta','payload':{'id':'chat','cwd':directory,'cli_version':'0.167.0'}}
            def write(code):
                item={'type':'event_msg','payload':{'type':'item_completed','turn_id':'turn',
                      'item':{'type':'CommandExecution','id':'cmd','status':'completed','exit_code':code}}}
                path.write_text(json.dumps(meta)+'\n'+json.dumps(item)+'\n')
            write(0)
            before=read_turn(path,'chat','turn',directory,sessions_root=root,include_task=False)
            write(1)
            after=read_turn(path,'chat','turn',directory,sessions_root=root,include_task=False,cursor=before['cursor'])
            self.assertEqual(after['commands']['cmd']['exit_code'],1)
    def test_large_history_and_partial_append_preserve_only_current_turn_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'session.jsonl'
            meta = {'type':'session_meta','payload':{'id':'chat','cwd':directory,'cli_version':'0.167.0'}}
            complete = {'type':'event_msg','payload':{'type':'item_completed','turn_id':'current',
                        'item':{'type':'CommandExecution','id':'command','status':'completed','exit_code':0}}}
            with path.open('w') as stream:
                stream.write(json.dumps(meta)+'\n')
                stream.write(json.dumps({'old':'x'*(17*1024*1024)})+'\n')
                stream.write(json.dumps(complete)+'\n')
                stream.write('{"type":')
            first = read_turn(path,'chat','current',directory,sessions_root=root,include_task=False)
            self.assertEqual(first['commands']['command']['exit_code'], 0)
            position = first['cursor']['offset']
            with path.open('a') as stream:
                stream.write('"ignored"}\n')
            again = read_turn(path,'chat','current',directory,sessions_root=root,include_task=False,cursor=first['cursor'])
            self.assertEqual(again['commands'], first['commands'])
            self.assertGreater(again['cursor']['offset'], position)
            self.assertNotIn('old', json.dumps(again))
