import ast
import builtins
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
    def test_existing_receiver_config_uses_the_managed_global_sender_when_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Hermes 学习.app/Contents/MacOS/HermesLearningNotifier"
            shared = Path(directory) / "Hermes 全局共享.app/Contents/MacOS/HermesLearningNotifier"
            config = {"notifier": str(project)}
            self.assertEqual(receiver.batch_notifier(config, "global"), str(project))
            self.assertEqual(receiver.batch_notifier(config, "system"), str(project))
            shared.parent.mkdir(parents=True)
            shared.touch()
            for identifier in ("com.cy.hermes-learning-global-receipts", "com.cy.hermes-learning-global-receipts-v2"):
                with self.subTest(identifier=identifier):
                    (shared.parents[2] / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
                    self.assertEqual(receiver.batch_notifier(config, "global"), str(shared))
                    self.assertEqual(receiver.batch_notifier(config, "system"), str(shared))
                    self.assertEqual(receiver.batch_notifier(config, "project"), str(project))

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
                publication = {**approval, "id": "publication", "approval_subject": "publication",
                               "summary": "通用技能待审 · portable", "text": "publication detail",
                               "pending_entities": ["publication:portable"]}
                checking = {**review, "id": "checking", "kind": "publication.pending", "notification_scope": "global",
                            "text": "checking detail", "applied_entities": [], "pending_entities": ["skill:later"]}
                items = {item["id"]: item for item in (review, approval, skill_approval, publication, checking)}
                posted = []

                def remote(action, *args, body=None):
                    if action == "capabilities":
                        return {"protocol_version": protocol}
                    if action == "poll":
                        return list(items.values())
                    if action == "detail":
                        return items[args[1]]
                    if action == "sync":
                        return {"items": list(items.values()) if body["after"] == 0 else [], "cursor": len(items)}
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

                self.assertEqual(len(posted), 4)
                self.assertEqual(posted[0], {"summary": review["summary"], "scope": "/project", "text": review["text"],
                                             "notification_scope": "project"})
                fields = {key: value for key, value in posted[1].items() if key not in {"presentation", "entries"}}
                self.assertEqual(fields, {"summary": "[project] 2 项提案待审批", "scope": "/project",
                                             "text": "memory detail\n\nskill detail", "kind": "approval.required",
                                             "project_approval": True, "notification_scope": "project"})
                self.assertEqual(posted[1]["presentation"]["title"], "⚠️ 项目待审批")
                self.assertEqual([item["text"] for item in posted[1]["entries"]], ["memory detail", "skill detail"])
                self.assertEqual(posted[2], {"summary": publication["summary"], "scope": "/project",
                                             "text": publication["text"], "kind": "approval.required",
                                             "project_approval": True, "approval_subject": "publication",
                                             "notification_scope": "global"})
                self.assertEqual(posted[3], {"summary": checking["summary"], "scope": "/project",
                                             "text": "checking detail", "notification_scope": "global",
                                             "kind": "publication.pending"})

    def test_both_protocols_prioritize_an_anomaly_and_keep_each_readable_entry(self):
        for protocol in (1, 2):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                client = receiver.Receiver({"state": directory, "host": "devbox", "bridge": "/bridge",
                                            "settings": "/settings", "notifier": "/notifier"})
                self.addCleanup(client.db.close)
                notices = {
                    "success": {"title": "已生效 1 项", "subtitle": "project", "body": "校验构建版本", "tone": "success", "sections": []},
                    "failed": {"title": "已达到重试上限", "subtitle": "project", "body": "连接暂时中断", "tone": "error", "sections": []},
                }
                items = [{"id": name, "scope": "/project", "summary": name, "kind": "review.finished", "status": name,
                          "text": name + " detail", "presentation": display} for name, display in notices.items()]

                def remote(action, *args, body=None):
                    if action == "capabilities":
                        return {"protocol_version": protocol}
                    if action == "poll":
                        return [{key: value for key, value in item.items() if key not in {"text", "presentation"}} for item in items]
                    if action == "detail":
                        return next(item for item in items if item["id"] == args[1])
                    self.assertEqual(action, "sync")
                    return {"items": items, "cursor": 2}

                with patch.object(client, "remote", side_effect=remote), \
                     patch.object(receiver.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                    client.poll()
                path, = client.db.execute("SELECT path FROM batches").fetchone()
                batch = json.loads(Path(path).read_text())
                self.assertEqual(batch["presentation"]["title"], "已达到重试上限")
                self.assertEqual(batch["presentation"]["body"], "连接暂时中断 · 2 条回执")
                self.assertEqual([entry["presentation"] for entry in batch["entries"]], list(notices.values()))
                self.assertEqual(batch["text"], "success detail\n\nfailed detail")

    def test_receiver_and_macos_entrypoints_work_with_python39_zip_and_syntax(self):
        root = Path(__file__).resolve().parents[1]
        checked = sorted((root / "desktop").glob("*.py")) + [
            root / "src/hermes_codex_learning/macos_bundle.py",
            root / "src/hermes_codex_learning/store.py",
        ]
        for path in checked:
            tree = ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "zip":
                    self.assertEqual(node.keywords, [], f"{path} uses Python 3.10+ zip keyword arguments")

        real_zip = builtins.zip

        def py39_zip(*iterables, **kwargs):
            if kwargs:
                raise TypeError("zip() takes no keyword arguments")
            return real_zip(*iterables)

        with tempfile.TemporaryDirectory() as directory:
            client = receiver.Receiver({"state": directory, "host": "devbox", "bridge": "/bridge",
                                        "settings": "/settings", "notifier": "/notifier"})
            self.addCleanup(client.db.close)
            item = {"id": "n1", "scope": "system", "notification_scope": "system", "kind": "update.notice",
                    "status": "pending", "summary": "已切换", "text": "detail",
                    "applied_entities": [], "pending_entities": []}
            with patch.object(builtins, "zip", side_effect=py39_zip), \
                 patch.object(client, "remote", side_effect=lambda action, *a, **kw:
                              {"protocol_version": 2} if action == "capabilities" else {"items": [item], "cursor": 1}), \
                 patch.object(receiver.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as display:
                client.poll()
                self.assertEqual(display.call_count, 1)
                with self.assertRaises(ValueError):
                    client.write_batch("system", [item], [])
