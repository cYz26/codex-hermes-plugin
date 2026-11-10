import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("desktop_receiver", Path(__file__).resolve().parents[1] / "desktop/receive.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class DesktopReceiverTests(unittest.TestCase):
    def test_reconnect_deduplicates_and_batches_only_within_a_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            client = receiver.Receiver({"state": directory, "host": "devbox", "bridge": "/bridge",
                                        "settings": "/settings", "notifier": "/notifier"})
            items = [{"id": name, "scope": scope, "summary": "fixture", "applied_entities": ["skill:one"], "pending_entities": []}
                     for name, scope in [("a1", "/a"), ("a2", "/a"), ("b1", "/b")]]
            acknowledgements = []
            def remote(action, *args):
                if action == "capabilities":
                    return {"protocol_version": 1}
                if action == "poll":
                    return items
                if action == "detail":
                    return {"text": "synthetic detail"}
                acknowledgements.append(args)
                return {}
            def post(argv, **kwargs):
                self.assertEqual(argv[:2], ["/notifier", "--post"])
                Path(argv[2] + ".sent").write_text("accepted")
                return SimpleNamespace(returncode=0)
            with patch.object(client, "remote", side_effect=remote), patch.object(receiver.subprocess, "run", side_effect=post) as display:
                client.poll()
                client.poll()
            self.assertEqual(display.call_count, 2)
            self.assertEqual(len(acknowledgements), 3)
            rows = client.db.execute("SELECT scope,path FROM batches").fetchall()
            self.assertEqual({row[0] for row in rows}, {"/a", "/b"})
            a = json.loads(Path(next(path for scope, path in rows if scope == "/a")).read_text())
            self.assertIn("1 项经验生效", a["summary"])
            client.db.close()
