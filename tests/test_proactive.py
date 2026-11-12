import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class ProactiveTests(unittest.TestCase):
    def test_an_old_queued_intent_does_not_acknowledge_newer_feedback(self):
        from hermes_codex_learning.scheduling import flush
        from hermes_codex_learning.store import digest
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=Settings(root/'home',root,root,Path('/usr/bin/python3'))
            intent={'handle':'chat','revision':1,'status':'queued'}
            api=SimpleNamespace(version=1,maintenance=lambda **kw:{},read=lambda **kw:{'intents':[intent]})
            bridge=Bridge(settings,api)
            try:
                identity=digest(['chat','feedback'])
                bridge.store.db.execute('INSERT INTO review_priority VALUES (?,?,?,?,?,NULL)',(identity,'chat','r','user_followup',1))
                bridge.store.db.commit()
                bridge.store.set('observation-receipt:'+identity,{'handle':'chat','revision':2})
                flush(bridge.store,api)
                self.assertIsNone(bridge.store.db.execute('SELECT requested FROM review_priority').fetchone()[0])
                intent['revision']=2
                flush(bridge.store,api)
                self.assertIsNotNone(bridge.store.db.execute('SELECT requested FROM review_priority').fetchone()[0])
            finally:bridge.store.close()
    def test_natural_followup_enters_review_without_a_correction_keyword(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/'home', root, root, Path('/usr/bin/python3'), proactive=True)
            flushes = []
            api = SimpleNamespace(open=lambda source:{'handle':source['chat']}, observe=lambda *a,**kw:None,
                                  maintenance=lambda **kw:flushes.append(kw) or {})
            bridge = Bridge(settings,api)
            try:
                for turn,prompt in [('first','Compare deployment options'),('second','还要结合现有的平台能力一起看看')]:
                    hook(dict(hook_event_name='UserPromptSubmit',prompt=prompt,cwd=directory,session_id='chat',turn_id=turn),settings)
                    hook(dict(hook_event_name='Stop',last_assistant_message='Options changed',cwd=directory,session_id='chat',turn_id=turn),settings)
                bridge.ingest()
                bridge.store.set('last_idle_request',time.time())
                bridge.idle_reviews()
                self.assertEqual(flushes,[{'handle':'chat','idle_flush':True}])
            finally:
                bridge.store.close()

    def test_late_prompt_preserves_the_real_terminal_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            settings=Settings(root/'home',root,root,Path('/usr/bin/python3'),proactive=True)
            observed=[]
            api=SimpleNamespace(open=lambda source:{'handle':'chat'},observe=lambda handle,body,**kw:observed.append(body))
            bridge=Bridge(settings,api)
            try:
                hook(dict(hook_event_name='Stop',last_assistant_message='The added source changes the choice',cwd=directory,session_id='chat',turn_id='late'),settings)
                hook(dict(hook_event_name='UserPromptSubmit',prompt='Please include the additional source',cwd=directory,session_id='chat',turn_id='late'),settings)
                bridge.ingest()
                self.assertEqual(len(observed),1)
                self.assertEqual(observed[0]['status'],'completed')
            finally:
                bridge.store.close()

    def test_correction_bypasses_idle_delay_once_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/'home', root, root, Path('/usr/bin/python3'), proactive=True)
            flushes = []
            api = SimpleNamespace(open=lambda source: {'handle':source['chat']},
                                  observe=lambda *a, **kw: None,
                                  maintenance=lambda **kw: flushes.append(kw) or {'intent':'fixture'})
            bridge = Bridge(settings, api)
            try:
                for chat, prompt in [('normal','Inspect the module'), ('correction','Correction: normalize before matching')]:
                    for event in [{'hook_event_name':'UserPromptSubmit','prompt':prompt},
                                  {'hook_event_name':'Stop','last_assistant_message':'done'}]:
                        hook(dict(event,cwd=directory,session_id=chat,turn_id='turn'), settings)
                bridge.ingest()
                bridge.store.set('last_idle_request', time.time())
                bridge.idle_reviews()
                self.assertEqual(flushes, [{'handle':'correction','idle_flush':True}])
                bridge.idle_reviews()
            finally:
                bridge.store.close()
            bridge = Bridge(settings, api)
            try:
                bridge.idle_reviews()
                self.assertEqual(len(flushes), 1)
            finally:
                bridge.store.close()
