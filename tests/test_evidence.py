import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.evidence import organize_review_history
from hermes_codex_learning.store import Store, digest, dumps


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.settings=Settings(self.root/'home',self.root,self.root,Path('/usr/bin/python3'),
                               data_policy={'enabled':True,'synthetic_roots':[str(self.root)]})
        self.store=Store(self.settings.state)
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE observations(id,handle,turn,revision,body,messages)')
        self.api=SimpleNamespace(_service=SimpleNamespace(db=self.db,lock=threading.RLock()))
        self.source={'host':'host','project':str(self.root),'chat':'chat'}
        self.messages=[]

    def tearDown(self):
        self.db.close();self.store.close();self.temp.cleanup()

    def add(self,turn,user,answer,revision,*,context=None,supplement=False):
        old=self.db.execute('SELECT body FROM observations WHERE turn=? ORDER BY revision LIMIT 1',(turn,)).fetchone()
        request_at=json.loads(old[0])['evidence_provenance']['request_at'] if old else revision
        body={'source':{**self.source,'turn':turn},'user':user,'assistant':answer,'status':'completed','tools':[],
              'evidence_provenance':{'request_at':request_at,'body_retained':True},'host_context':context or {}}
        messages=[{'role':'user','content':'[Supplemental evidence]' if supplement else user},
                  {'role':'assistant','content':answer}]
        for i,m in enumerate(messages):m['message_uid']=digest([turn,revision,i])
        self.db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?)',('event-'+str(revision),'h',turn,revision,dumps(body),dumps(messages)))
        self.messages+=messages
        return body

    def replay(self,revision):
        return organize_review_history(self.api,'h',self.source,self.messages,
                                       {'id':'intent','revision':revision},self.settings)

    def test_late_result_cannot_be_the_prior_conclusion_of_an_earlier_followup(self):
        self.add('first','Compare technical alternatives','Original public ranking.',1)
        self.add('second','Also compare internal capabilities.','Internal reuse changes the preference.',2)
        self.add('first','Compare technical alternatives','Late result supplement.',3,supplement=True)
        messages,report=self.replay(3)
        request=next(m for m in messages if m.get('evidence_ref')=='T2.request')
        self.assertIn('T1.result.1',request['content'])
        self.assertNotIn('"previous_result":"T1.result"',request['content'])

    def test_goal_prior_approach_addition_and_changed_result_have_ordered_source_refs(self):
        self.add('first','Compare deployment alternatives','Only public sources were compared.',1)
        self.add('second','Also examine our internal capabilities.','The existing internal platform changes the preferred choice.',2)
        self.add('second','Also examine our internal capabilities.','The existing internal platform changes the preferred choice.',3,supplement=True)
        self.add('future','FUTURE_REQUEST_MUST_NOT_ENTER','FUTURE_RESULT_MUST_NOT_ENTER',4)
        messages,report=self.replay(3)
        text=dumps(messages)
        self.assertIn('Compare deployment alternatives',text)
        self.assertIn('Only public sources were compared',text)
        self.assertIn('previous_result',text)
        self.assertIn('T1.result',text)
        self.assertEqual(text.count('Also examine our internal capabilities.'),1)
        self.assertNotIn('Supplemental evidence',text)
        self.assertNotIn('FUTURE_',text)
        self.assertEqual([item['source']['turn'] for item in report['turns']],['first','second'])
        self.assertEqual(report['turns'][1]['source_events'],['event-2','event-3'])

    def parent(self,chat,turn,user,answer,start,end,*,mode='semantic-v2',host='host'):
        path=self.root/'sessions'/f'{chat}.jsonl';path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_text(dumps({'type':'session_meta','payload':{'id':chat,'cwd':str(self.root),'cli_version':'0.167.0'}})+'\n')
        with path.open('a') as stream:
            for ordinal,role in [(start,'user'),(end,'assistant')]:
                stream.write(dumps({'type':'response_item','ordinal':ordinal,'payload':{
                    'role':role,'id':role+'-'+turn,'internal_chat_message_metadata_passthrough':{'turn_id':turn}}})+'\n')
        body={'source':{'host':host,'project':str(self.root),'chat':chat,'turn':turn},'user':user,'assistant':answer,
              'tools':{},'status':'completed','_evidence_mode':mode}
        self.store.db.execute('INSERT INTO turns(id,payload,revision,activity,observed_revision) VALUES (?,?,?,?,?)',
                              (digest([chat,turn]),dumps(body),'r',1,'r'))
        self.store.db.commit()
        self.store.capture({'session_id':chat,'turn_id':turn,'bridge_host':host,'bridge_project':str(self.root),'transcript_path':str(path)})

    def child(self):
        self.add('child','Please include internal capability in the comparison.','The added comparison changes the choice.',1,
                 context={'relation':{'status':'verified','kind':'sidechat','parent_chat':'parent','fork_ordinal_exclusive':25,'basis':'host_session_meta'}})

    def test_sidechat_uses_only_admitted_parent_context_before_the_real_fork(self):
        self.parent('parent','prior','Compare platform alternatives.','Only external evidence was considered.',10,20)
        self.parent('parent','later','AFTER_FORK_MUST_NOT_ENTER','AFTER_FORK_RESULT',30,40)
        self.parent('unrelated','other','UNRELATED_MUST_NOT_ENTER','UNRELATED_RESULT',1,2)
        self.child()
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root)}):
            messages,report=self.replay(1)
        text=dumps(messages)
        self.assertIn('Only external evidence was considered',text)
        self.assertIn('scope',text)
        self.assertIn('parent',text)
        self.assertNotIn('AFTER_FORK_',text)
        self.assertNotIn('UNRELATED_',text)
        self.assertEqual(report['parent_context'][0]['source']['turn'],'prior')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)

    def test_unretained_and_restricted_parent_bodies_remain_explicit_gaps(self):
        self.parent('parent','old','UNRETAINED_BODY','UNRETAINED_RESULT',1,2,mode='workflow-v1')
        self.parent('parent','restricted','RESTRICTED_BODY','RESTRICTED_RESULT',10,20)
        self.store.set('withheld-turn:'+digest(['parent','restricted']),'explicitly_restricted_material')
        self.child()
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root)}):
            messages,report=self.replay(1)
        self.assertNotIn('UNRETAINED_BODY',dumps(messages))
        self.assertNotIn('RESTRICTED_BODY',dumps(messages))
        self.assertEqual(report['parent_context'],[])
        reasons={item['reason'] for item in report['coverage_gaps']}
        self.assertIn('parent_turn_body_not_retained',reasons)
        self.assertIn('parent_turn_not_admitted',reasons)
