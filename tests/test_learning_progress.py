import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.normalize import merge_event
from hermes_codex_learning.store import digest,dumps


class LearningProgressTests(unittest.TestCase):
    def test_status_lookup_is_bounded_before_joining_many_retained_turns(self):
        for i in range(200):
            self.event('UserPromptSubmit', turn=f'many-{i}', prompt='Synthetic retained goal')
            self.event('Stop', turn=f'many-{i}', last_assistant_message='Synthetic retained result')
        while self.bridge.store.pending():
            self.bridge.ingest()
        instructions = 0
        def bounded():
            nonlocal instructions
            instructions += 1000
            return instructions > 200000
        self.bridge.store.db.set_progress_handler(bounded, 1000)
        try:
            from hermes_codex_learning.progress import summarize
            result = summarize(self.bridge.store, project=str(self.root), chat='chat', limit=5)
            self.assertEqual(len(result['turns']), 5)
            self.assertTrue(result['has_more'])
            self.assertTrue(all(row['admission']['state']=='admitted' for row in result['turns']))
        finally:
            self.bridge.store.db.set_progress_handler(None, 0)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.settings=Settings(self.root/'home',self.root,self.root,Path('/usr/bin/python3'),proactive=True)
        self.observed=[]
        def observe(handle,body,**kw):
            self.observed.append(body)
            return {'handle':handle,'revision':len(self.observed)}
        self.api=SimpleNamespace(version=1,open=lambda source:{'handle':source['chat']},observe=observe)
        self.bridge=Bridge(self.settings,self.api)

    def tearDown(self):
        self.bridge.store.close();self.temp.cleanup()

    def event(self,kind,turn='t',chat='chat',**fields):
        hook(dict(hook_event_name=kind,session_id=chat,turn_id=turn,cwd=str(self.root),**fields),self.settings)

    def status(self,**fields):
        return self.bridge.rpc('learning_status',{'cwd':str(self.root),'chat':'chat',**fields})['progress']

    def test_positions_distinguish_capture_review_candidate_publication_and_recall(self):
        self.event('UserPromptSubmit',prompt='Compare technical research alternatives')
        self.event('Stop',last_assistant_message='The comparison changed the preferred alternative')
        pending=self.status()['turns'][0]
        self.assertEqual(pending['consumption']['state'],'pending')
        self.assertEqual(pending['review']['state'],'not_observed')
        self.bridge.ingest()
        observed=self.status()['turns'][0]
        self.assertEqual(observed['consumption']['state'],'consumed')
        self.assertEqual(observed['admission']['state'],'admitted')
        self.assertEqual(observed['review']['state'],'awaiting_review')
        name='learned-comparative-research'
        self.bridge.store.set('review-progress:chat',{'state':'completed','intent':'intent','revision':1,
            'input':{'turns':[{'ref':'T1','source':{'chat':'chat','turn':'t'}}]},'versions':{name:'new'},
            'candidates':[{'state':'accepted','name':name,'tool':'skill_manage','classification':'procedure',
                           'basis':[{'ref':'T1.request','quote':'Compare technical research alternatives'}]}]})
        self.bridge.store.set('publications-v1',{name:{'artifact':{'version':'old'}}})
        self.bridge.store.set('publication-decisions',{name:{'allowed':False,'reason':'awaiting_validation'}})
        self.bridge.store.set('recall-offer:'+digest(['chat','t']),{'names':[name],'context_returned':True})
        reviewed=self.status()['turns'][0]
        self.assertEqual(reviewed['review']['state'],'completed')
        self.assertEqual(reviewed['publication'][0]['state'],'withheld')
        self.assertEqual(reviewed['recall']['body_deliveries'],[])
        self.assertEqual(reviewed['recall']['application_evidence'],[])
        self.assertEqual(reviewed['execution']['correctness'],'not_inferred')
        self.event('UserPromptSubmit',turn='new',prompt='Also compare existing internal capabilities')
        self.event('Stop',turn='new',last_assistant_message='Internal evidence changes the recommendation')
        self.bridge.ingest()
        newer=self.status(turn='new')['turns'][0]
        self.assertEqual(newer['review']['state'],'awaiting_review')
        self.assertFalse(newer['review']['covers_native_revision'])

    def test_dialogue_feedback_does_not_wait_for_or_verify_a_running_tool(self):
        self.event('UserPromptSubmit',turn='first',prompt='Compare technical alternatives')
        self.event('Stop',turn='first',last_assistant_message='Public alternatives compared')
        self.event('UserPromptSubmit',turn='feedback',prompt='还要比较现有内部能力')
        self.event('PreToolUse',turn='feedback',tool_use_id='running',tool_name='exec_command',tool_input={'cmd':'fixture'})
        self.event('Stop',turn='feedback',last_assistant_message='The additional comparison changes the preferred option')
        self.bridge.ingest()
        body=self.observed[-1]
        self.assertEqual(body['learning_basis'],'dialogue_with_unresolved_execution')
        self.assertEqual(body['tools'][0]['status'],'running')
        self.assertIn('execution_results_unresolved',body['coverage'])
        status=self.status(turn='feedback')['turns'][0]
        self.assertIn('user_followup',status['dialogue_signal'])
        self.assertEqual(status['execution']['correctness'],'not_inferred')

    def test_unavailable_old_inputs_do_not_starve_a_later_recoverable_turn(self):
        source={'host':self.bridge.store.get('host_id'),'project':str(self.root),'chat':'chat'}
        for i in range(10):
            body={'source':{**source,'turn':f'old-{i}'},'user':'','assistant':'','tools':{},'status':'completed','coverage':[]}
            self.bridge.store.db.execute('INSERT INTO turns(id,payload,revision,activity) VALUES (?,?,?,?)',
                                        (digest(['chat',f'old-{i}']),dumps(body),'r',i))
        self.bridge.store.db.commit()
        self.event('UserPromptSubmit',turn='recoverable',prompt='Include additional internal evidence')
        self.event('Stop',turn='recoverable',last_assistant_message='The recommendation changes')
        for row in self.bridge.store.pending():merge_event(self.bridge.store,row)
        self.bridge.retry_completed_inputs()
        self.assertEqual(self.observed,[])
        self.bridge.store.set('last_input_retry',0)
        self.bridge.retry_completed_inputs()
        self.assertEqual(len(self.observed),1)
        self.assertEqual(self.observed[0]['source']['turn'],'recoverable')

    def test_reviewing_an_older_revision_keeps_the_newer_feedback_priority(self):
        old,new=digest(['chat','old']),digest(['chat','new'])
        for key,revision in ((old,1),(new,2)):
            self.bridge.store.db.execute('INSERT INTO review_priority VALUES (?,?,?,?,?,?)',(key,'chat','r','user_followup',1,2))
            self.bridge.store.set('observation-receipt:'+key,{'handle':'chat','revision':revision})
        self.bridge.store.db.commit()
        self.bridge.clear_reviewed_priority({'handle':'chat','revision':1},3)
        self.assertEqual([row[0] for row in self.bridge.store.db.execute('SELECT id FROM review_priority')],[new])
