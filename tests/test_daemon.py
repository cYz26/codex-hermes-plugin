import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.notices import NoticeQueue
from hermes_codex_learning.notify import persist_receipt


class NotificationTests(unittest.TestCase):
    def test_new_pending_proposals_get_a_distinct_project_reminder_on_both_transports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "fixture-project"
            bridge = Bridge(Settings(root, project, root, Path(sys.executable), notifications=True), object())
            self.addCleanup(bridge.store.close)
            pending = root / "pending" / "memory"
            pending.mkdir(parents=True)
            (pending / "11111111.json").write_text('{"id":"11111111"}')
            fact = {"tool": "memory", "target": "memory", "result": {"pending_id": "11111111"}}
            event = {"id": "review-1", "kind": "review.finished", "project": str(project),
                     "run_status": "failed", "staged": [fact, fact], "receipt": "Original review receipt"}

            bridge.emit(event)
            bridge.emit({**event, "staged": []})  # Replaying an attempt keeps its first notice.
            with patch("hermes_codex_learning.daemon.sys.platform", "darwin"), \
                 patch("hermes_codex_learning.daemon.macos_notification") as post:
                post.return_value = {"dispatched": True}
                bridge.notifications()
                bridge.notifications()
            local = [call.args[1] for call in post.call_args_list]
            self.assertEqual([item["kind"] for item in local], ["approval.required", "review.finished"])
            self.assertNotEqual(local[0]["id"], local[1]["id"])
            self.assertEqual(local[1]["receipt"], "Original review receipt")
            self.assertIn("1 项记忆提案待审批", local[0]["message"])
            receipt = json.loads(persist_receipt(bridge.settings.state, local[0]).read_text())
            self.assertEqual(receipt["kind"], "approval.required")
            self.assertTrue(receipt["project_approval"])

            queue = NoticeQueue(bridge.settings.state)
            self.addCleanup(queue.close)
            items = queue.sync("fixture")["items"]
            self.assertEqual([item["id"] for item in items], [item["id"] for item in local])
            reminder = items[0]
            self.assertEqual((reminder["kind"], reminder["scope"], reminder["status"]),
                             ("approval.required", str(project), "pending"))
            self.assertEqual(reminder["pending_entities"], ["memory:11111111"])
            self.assertEqual(items[1]["pending_entities"], [])
            self.assertTrue(queue.detail(reminder["id"], str(project))["project_approval"])
            self.assertTrue((pending / "11111111.json").is_file())
            saved = bridge.store.db.execute("SELECT payload FROM notices WHERE id='review-1'").fetchone()[0]
            self.assertEqual(json.loads(saved)["staged"], event["staged"])

    def test_pending_only_review_sends_one_reminder_for_saved_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bridge = Bridge(Settings(root, root, root, Path(sys.executable)), object())
            self.addCleanup(bridge.store.close)
            pending = root / "pending" / "skills"
            pending.mkdir(parents=True)
            (pending / "11111111.json").write_text('{"id":"11111111"}')
            bridge.emit({"id": "review-2", "kind": "review.finished", "project": str(root),
                         "run_status": "completed", "staged": [
                             {"tool": "skill_manage", "result": {"pending_id": "11111111"}},
                             {"tool": "memory", "result": {"pending_id": "22222222"}},
                         ]})

            queue = NoticeQueue(bridge.settings.state)
            self.addCleanup(queue.close)
            items = queue.sync("fixture")["items"]
            self.assertEqual([item.get("kind") for item in items], ["approval.required"])
            self.assertEqual(items[0]["pending_entities"], ["skills:11111111"])
            self.assertFalse(items[0]["project_approval"])
            self.assertIn("/skills pending", items[0]["text"])

    def test_approval_banner_and_review_summary_keep_their_own_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bridge = Bridge(Settings(root, root, root, Path(sys.executable)), object())
            self.addCleanup(bridge.store.close)
            pending = root / "pending" / "memory"
            pending.mkdir(parents=True)
            (pending / "abcdef12.json").write_text('{"id":"abcdef12"}')
            (pending / "1234abcd.json").write_text('{"id":"1234abcd"}')
            bridge.emit({"id": "review-3", "kind": "review.finished", "project": str(root),
                         "run_status": "completed", "started_at": 1, "finished_at": 2,
                         "review_summary": "核对了项目规则。", "applied_entities": ["skill:fixture"],
                         "staged": [{"tool": "memory", "call_id": "patch", "target": "memory",
                                     "result": {"pending_id": "abcdef12"}},
                                    {"tool": "memory", "target": "user",
                                     "result": {"pending_id": "1234abcd"}}]},
                        calls={("memory", "patch"): {"action": "patch", "old_text": "original fixture",
                                                       "content": "proposed fixture"}})

            queue = NoticeQueue(bridge.settings.state)
            self.addCleanup(queue.close)
            reminder, review = queue.sync("fixture")["items"]
            self.assertIn("1 项变更生效", review["summary"])
            self.assertNotIn("待审批", review["summary"])
            self.assertIn("等待批准", review["text"])
            self.assertIn("拟写入的内容\nproposed fixture", review["text"])
            self.assertIn("1 项记忆提案、1 项用户档案提案待审批", reminder["summary"])
            self.assertIn("/memory pending", reminder["text"])
            self.assertNotIn("proposed fixture", reminder["text"])

    @unittest.skipIf(sys.platform == "darwin", "Non-macOS service behavior")
    def test_non_macos_never_dispatches_osascript_even_with_legacy_notifications_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = Bridge(Settings(root, root, root, Path(sys.executable), notifications=True), object())
            try:
                bridge.store.notice({"id": "fixture", "kind": "review.finished", "run_status": "completed",
                                     "applied": [{"name": "fixture"}], "message": "Synthetic result"})
                with patch("hermes_codex_learning.daemon.macos_notification", return_value={"dispatched": False}) as notify:
                    bridge.notifications()
                notify.assert_not_called()
            finally:
                bridge.store.close()
