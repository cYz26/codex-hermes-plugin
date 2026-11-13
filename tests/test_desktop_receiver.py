import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("desktop_receiver", Path(__file__).resolve().parents[1] / "desktop/receive.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class DesktopReceiverTests(unittest.TestCase):
    def test_delivery_resolves_legacy_app_while_canonical_app_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            configured = str(app / "Contents/MacOS/HermesLearningNotifier")
            legacy = app.parent / ".hermes-learning-legacy" / app.name
            (legacy / "Contents/MacOS").mkdir(parents=True)
            (legacy / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.cy.hermes-learning-notifier"}))
            self.assertEqual(receiver.notifier_binary(configured), str(legacy / "Contents/MacOS/HermesLearningNotifier"))

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

    def test_approval_reminders_stay_separate_in_both_receiver_protocols(self):
        for protocol in (1, 2):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                client = receiver.Receiver({"state": directory, "host": "devbox", "bridge": "/bridge",
                                            "settings": "/settings", "notifier": "/notifier"})
                self.addCleanup(client.db.close)
                review = {"id": "review", "scope": "/project", "summary": "普通学习回执",
                          "text": "review detail", "applied_entities": ["skill:one"], "pending_entities": []}
                approval = {**review, "id": "approval", "kind": "approval.required", "text": "memory detail",
                            "project_approval": True, "applied_entities": [], "pending_entities": ["memory:11111111"]}
                skill_approval = {**approval, "id": "skills", "text": "skill detail", "project_approval": False,
                                  "pending_entities": ["skills:22222222"]}
                items = {item["id"]: item for item in (review, approval, skill_approval)}
                posted = []

                def remote(action, *args, body=None):
                    if action == "capabilities":
                        return {"protocol_version": protocol}
                    if action == "poll":
                        return list(items.values())
                    if action == "detail":
                        return items[args[1]]
                    if action == "sync":
                        return {"items": list(items.values()) if body["after"] == 0 else [], "cursor": 3}
                    self.assertEqual(action, "ack")
                    return {}

                def post(argv, **_):
                    path = Path(argv[2])
                    posted.append(json.loads(path.read_text()))
                    path.with_suffix(".json.sent").write_text("accepted")
                    return SimpleNamespace(returncode=0)

                with patch.object(client, "remote", side_effect=remote), \
                     patch.object(receiver.subprocess, "run", side_effect=post):
                    client.poll()
                    client.poll()

                self.assertEqual(len(posted), 2)
                self.assertEqual(posted[0], {"summary": review["summary"], "scope": "/project", "text": review["text"]})
                self.assertEqual(posted[1], {"summary": "[project] 2 项提案待审批", "scope": "/project",
                                             "text": "memory detail\n\nskill detail", "kind": "approval.required",
                                             "project_approval": True})
