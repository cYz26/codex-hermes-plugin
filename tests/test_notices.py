import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.notices import NoticeQueue, entities
from hermes_codex_learning.notify import message
from hermes_codex_learning.store import atomic_json


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = NoticeQueue(self.root / "bridge")

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def event(self):
        return {"id": "review-1", "kind": "review.finished", "project": "/work/a", "run_status": "completed",
                "message": "one skill", "receipt": "PRIVATE-NOTICE-BODY",
                "applied": [{"tool": "skill_manage", "operation": {"name": "portable", "file_path": "SKILL.md"}},
                            {"tool": "skill_manage", "operation": {"name": "portable", "file_path": "references/a.md"}}]}

    def test_skill_files_count_as_one_entity(self):
        self.assertEqual(entities(self.event()["applied"]), ["skill:portable"])

    def test_disconnect_replays_until_sent_and_restart_does_not_repeat(self):
        self.queue.publish(self.event())
        first = self.queue.poll("desktop")
        self.assertEqual(first, self.queue.poll("desktop"))
        self.queue.acknowledge("desktop", "review-1", "/work/a", "sent")
        self.assertEqual(self.queue.poll("desktop"), [])
        self.assertEqual(len(self.queue.poll("chat-ui")), 1)
        self.queue.close()
        self.queue = NoticeQueue(self.root / "bridge")
        self.assertEqual(self.queue.poll("desktop"), [])
        self.assertEqual(self.queue.acknowledge("desktop", "review-1", "/work/a", "read")["visibility"], "opened")

    def test_private_receipt_requires_its_workspace(self):
        self.queue.publish(self.event())
        with self.assertRaises(PermissionError):
            self.queue.detail("review-1", "/work/b")
        self.assertEqual(self.queue.detail("review-1", "/work/a")["text"], "PRIVATE-NOTICE-BODY")

    def test_notice_arrival_and_detail_read_do_not_change_model_context(self):
        settings = Settings(self.root, self.root, self.root, Path("/usr/bin/python3"))
        atomic_json(settings.state / "context.json", {"summary": "business recall only", "version": "v1"})
        event = {"hook_event_name": "SessionStart", "cwd": str(self.root), "session_id": "chat"}
        hook(event, settings)  # Initial business recall has its own delivery cursor.
        before = hook(event, settings)
        cached = (settings.state / "context.json").read_bytes()
        self.queue.publish(self.event())
        self.queue.detail("review-1", "/work/a")
        after = hook(event, settings)
        self.assertEqual(before, after)
        self.assertEqual((settings.state / "context.json").read_bytes(), cached)
        self.assertNotIn("PRIVATE-NOTICE-BODY", str(after))

    def test_review_explanation_reaches_ssh_details_without_changing_the_receipt_schema(self):
        event = {**self.event(), "review_summary": "本轮依据回归结果补充了接口检查步骤。"}
        event.pop("receipt")
        event["message"] = message(event)
        self.queue.publish(event)

        item = self.queue.sync("desktop")["items"][0]
        detail = self.queue.detail(item["id"], item["scope"])

        self.assertIn(event["review_summary"], item["summary"])
        self.assertIn("复盘简述\n" + event["review_summary"], item["text"])
        self.assertIn("后续任务验证：尚未确认", item["text"])
        self.assertEqual(set(detail), {"id", "scope", "summary", "text"})
        self.assertEqual(item["status"], "applied")

    def test_success_without_changes_is_silent(self):
        event = self.event()
        event["applied"] = []
        self.queue.publish(event)
        self.assertEqual(self.queue.poll("desktop"), [])
