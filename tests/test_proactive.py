import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class ProactiveTests(unittest.TestCase):
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
