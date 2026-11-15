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
                bridge.store.db.execute('INSERT INTO review_priority VALUES (?,?,?,?,?,NULL)',(identity,'chat','r','user_correction',1))
                bridge.store.db.commit()
                bridge.store.set('observation-receipt:'+identity,{'handle':'chat','revision':2})
                flush(bridge.store,api)
                self.assertIsNone(bridge.store.db.execute('SELECT requested FROM review_priority').fetchone()[0])
                intent['revision']=2
                flush(bridge.store,api)
                self.assertIsNotNone(bridge.store.db.execute('SELECT requested FROM review_priority').fetchone()[0])
            finally:bridge.store.close()
    def test_natural_followup_does_not_request_review_even_after_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/'home', root, root, Path('/usr/bin/python3'), proactive=True)
            flushes = []
            api = SimpleNamespace(open=lambda source:{'handle':source['chat']}, observe=lambda *a,**kw:None,
                                  maintenance=lambda **kw:flushes.append(kw) or {})
            bridge = Bridge(settings,api)
            try:
                for turn,prompt in [('first','Compare deployment options'),('second','Actually, where are the deployment options?')]:
                    hook(dict(hook_event_name='UserPromptSubmit',prompt=prompt,cwd=directory,session_id='chat',turn_id=turn),settings)
                    hook(dict(hook_event_name='Stop',last_assistant_message='Options changed',cwd=directory,session_id='chat',turn_id=turn),settings)
                # Earlier versions of the installed hook labeled leading
                # "Actually" as a correction, even when it was a question.
                with bridge.store.db:
                    bridge.store.db.execute("INSERT INTO learning_signals VALUES (?,?,?,?)",
                                            ("chat", "second", "user_correction", time.time()))
                bridge.ingest()
                with bridge.store.db:
                    bridge.store.db.execute("UPDATE turns SET activity=0")
                    bridge.store.db.execute("INSERT INTO review_priority VALUES (?,?,?,?,?,NULL)",
                                            ("legacy-followup", "chat", "r", "user_followup", 1))
                bridge.idle_reviews()
                self.assertEqual(flushes, [])
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

    def test_same_test_recovery_uses_host_order_and_requires_a_new_test_start(self):
        for test_started_before_edit in (False, True):
            with self.subTest(test_started_before_edit=test_started_before_edit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = Settings(root / "home", root, root, Path("/usr/bin/python3"), proactive=True)
                requests = []
                api = SimpleNamespace(open=lambda source: {"handle": source["chat"]},
                                      observe=lambda *args, **kwargs: {},
                                      maintenance=lambda **kwargs: requests.append(kwargs))
                bridge = Bridge(settings, api)
                try:
                    def event(kind, **fields):
                        hook(dict(hook_event_name=kind, cwd=directory, session_id="chat", turn_id="turn",
                                  **fields), settings)
                    def tool(kind, name, call, args, **fields):
                        event(kind, tool_name=name, tool_use_id=call, tool_input=args, **fields)

                    invocation = {"command": "python3 -m unittest"}
                    patch_args = {"patch": "synthetic edit"}
                    event("UserPromptSubmit", prompt="Repair and rerun the test")
                    tool("PreToolUse", "Bash", "z-first", invocation)
                    tool("PostToolUse", "Bash", "z-first", invocation, tool_response={"exit_code": 1})
                    if test_started_before_edit:
                        tool("PreToolUse", "Bash", "m-second", invocation)
                    tool("PreToolUse", "apply_patch", "a-edit", patch_args)
                    tool("PostToolUse", "apply_patch", "a-edit", patch_args, tool_response={})
                    if not test_started_before_edit:
                        tool("PreToolUse", "Bash", "m-second", invocation)
                    tool("PostToolUse", "Bash", "m-second", invocation, tool_response={"exit_code": 0})
                    event("Stop", last_assistant_message="The test passed")
                    bridge.ingest()
                    bridge.idle_reviews()
                    expected = [] if test_started_before_edit else [{"handle": "chat", "idle_flush": True}]
                    self.assertEqual(requests, expected)
                finally:
                    bridge.store.close()

    def test_correction_queues_once_and_survives_restart(self):
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

    def test_queued_automatic_reviews_wait_for_chat_to_settle_but_explicit_does_not(self):
        from unittest.mock import patch

        from hermes_codex_learning.scheduling import MAX_WAIT_SECONDS, SETTLE_SECONDS, ordered_intents
        from hermes_codex_learning.store import Store

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.close)
            store.set("scope:handle", {"chat": "chat"})
            with store.db:
                store.db.execute("INSERT INTO latest_turns VALUES ('chat','latest',100)")
                store.db.execute("INSERT INTO turns VALUES ('latest',?,'r','r',100,100)",
                                 ('{"status":"completed"}',))
            automatic = {"id": "automatic", "handle": "handle", "revision": 1, "status": "queued",
                         "reason": "native_threshold", "skills": True}
            explicit = {**automatic, "id": "explicit", "reason": "user_request"}
            with patch("hermes_codex_learning.scheduling.time.time", return_value=101):
                self.assertEqual(ordered_intents(store, [automatic, explicit]), [explicit])
            with patch("hermes_codex_learning.scheduling.time.time", return_value=100+SETTLE_SECONDS):
                self.assertEqual(ordered_intents(store, [automatic]), [automatic])
            with store.db:
                store.db.execute("UPDATE turns SET payload=?", ('{"status":"running"}',))
            with patch("hermes_codex_learning.scheduling.time.time", return_value=100+MAX_WAIT_SECONDS):
                self.assertEqual(ordered_intents(store, [automatic]), [])
            with patch("hermes_codex_learning.scheduling.time.time", return_value=101+MAX_WAIT_SECONDS):
                self.assertEqual(ordered_intents(store, [automatic]), [automatic])
