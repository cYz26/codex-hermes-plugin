import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.store import Store, digest
from hermes_codex_learning.config import Settings


class RetentionTests(unittest.TestCase):
    def test_processed_body_is_removed_but_same_event_cannot_replay(self):
        from hermes_codex_learning.retention import run
        from hermes_codex_learning.normalize import merge_event
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/'home', root, root, Path('/usr/bin/python3'))
            store = Store(settings.state)
            event = {"hook_event_name":"UserPromptSubmit","cwd":directory,"session_id":"c","turn_id":"t","prompt":"BODY-TO-EXPIRE"}
            try:
                event_id = store.capture(event)
                row = store.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                data = merge_event(store,row)
                data.update(status="completed")
                old = time.time()-40*86400
                store.db.execute("UPDATE turns SET payload=?,observed_revision=revision,activity=?", (json.dumps(data),old))
                store.db.execute("UPDATE events SET delivered=?,created=?", (old,old))
                store.db.commit()
                source = {k:data["source"][k] for k in ("host","project","chat")}
                store.set("chat-handles:"+digest(source), ["handle"])
                proof = {"review_handle":"handle", "learning_evidence":{"state":"passed",
                         "claims":[{"basis":[{"ref":"T1.request", "quote":"BODY-TO-EXPIRE"}]}]}}
                store.set("semantic-skill-attestations", {"active":proof})
                store.set("archived-skill-origins", {"archived":{"attestation":proof}})
                api = SimpleNamespace(prune=lambda **kw: {"dry_run":kw["dry_run"]},
                                      read=lambda **kw: {"retired":True})
                preview = run(settings,store,api,dry_run=True)
                self.assertEqual(preview["eligible_turns"],1)
                self.assertIn("BODY-TO-EXPIRE", store.db.execute("SELECT payload FROM events").fetchone()[0])
                done = run(settings,store,api,dry_run=False)
                self.assertEqual(done["retired_turns"],1)
                self.assertEqual(store.capture(event),event_id)
                self.assertEqual(store.pending(),[])
                self.assertNotIn("BODY-TO-EXPIRE",store.db.execute("SELECT payload FROM turns").fetchone()[0])
                for key in ("semantic-skill-attestations", "archived-skill-origins"):
                    self.assertNotIn("BODY-TO-EXPIRE", json.dumps(store.get(key)))
                    self.assertIn("retired_source_quotes", json.dumps(store.get(key)))
            finally:
                store.close()
