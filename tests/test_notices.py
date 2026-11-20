import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.notices import NoticeQueue, entities
from hermes_codex_learning.macos_bundle import APP_EXECUTABLE, APP_ID, GLOBAL_APP_ID, global_notifier_app
from hermes_codex_learning.notify import macos_notification, message, persist_receipt
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

    def test_review_explanation_reaches_both_ssh_protocols_with_legacy_fields(self):
        event = {**self.event(), "review_summary": "本轮依据回归结果补充了接口检查步骤。"}
        event.pop("receipt")
        event["message"] = message(event)
        self.queue.publish(event)

        item = self.queue.sync("desktop")["items"][0]
        detail = self.queue.detail(item["id"], item["scope"])

        self.assertIn(event["review_summary"], item["summary"])
        self.assertIn("复盘简述\n" + event["review_summary"], item["text"])
        self.assertIn("后续任务验证：尚未确认", item["text"])
        self.assertTrue({"id", "scope", "summary", "text", "kind", "notification_scope"} <= set(detail))
        self.assertEqual(detail["notification_scope"], "project")
        self.assertEqual(item["status"], "applied")
        self.assertEqual(item["presentation"], detail["presentation"])
        banner = item["presentation"]
        self.assertEqual((banner["title"], banner["subtitle"], banner["body"], banner["tone"]),
                         ("已生效 1 项", "a", event["review_summary"], "success"))
        self.assertIn("技能 portable", banner["sections"][1]["text"])

    def test_review_presentations_separate_partial_effects_and_retry_states(self):
        from hermes_codex_learning.notice_presentation import notice_presentation

        retry = dict(state="waiting", reason="rate_limit", max_retries=3, next_retry_index=1, next_retry_at=2_000_000_000)
        event = {**self.event(), "run_status": "interrupted", "retry": retry}
        partial = notice_presentation(event)
        self.assertEqual(partial["title"], "复盘中断 · 1 项已生效")
        self.assertEqual(partial["retry_at"], retry["next_retry_at"])
        self.assertEqual(partial["body"], "模型限流 · 第 1/3 次")
        self.assertEqual([part["title"] for part in partial["sections"]], ["已生效变更", "异常与重试"])
        for state, title, tone in (
            ("waiting", "已安排自动重试", "waiting"),
            ("blocked", "自动重试已暂停", "warning"),
            ("exhausted", "已达到重试上限", "error"),
        ):
            with self.subTest(state=state):
                display = notice_presentation({**event, "applied_entities": [], "retry": {**retry, "state": state}})
                self.assertEqual((display["title"], display["tone"]), (title, tone))
                self.assertEqual("retry_at" in display, state == "waiting")
                self.assertNotIn("已生效变更", [part["title"] for part in display["sections"]])
        self.assertEqual(notice_presentation(self.event())["body"], "")

    def test_banner_uses_one_redacted_sentence_but_detail_keeps_the_complete_safe_summary(self):
        from hermes_codex_learning.notice_presentation import notice_presentation

        secret = "sk-" + "D" * 32
        source = f"核验{secret}后重新构建。适用于正式发布。"
        result = notice_presentation({**self.event(), "review_summary": source})
        self.assertEqual(result["body"], "核验[REDACTED]后重新构建。")
        self.assertIn("适用于正式发布。", result["sections"][0]["text"])
        self.assertNotIn(secret, str(result))

    def test_share_events_keep_display_scope_separate_from_approval_project(self):
        from hermes_codex_learning.notify import APPROVAL_REQUIRED

        for identifier, kind, source, subject, display, access in (
            ("learned", "review.finished", "/work/a", None, "project", "/work/a"),
            ("project-pending", APPROVAL_REQUIRED, "/work/a", None, "project", "/work/a"),
            ("waiting-for-check", "publication.pending", "/work/a", None, "global", "/work/a"),
            ("share-pending", APPROVAL_REQUIRED, "/work/a", "publication", "global", "/work/a"),
            ("shared", "publication.applied", None, None, "global", "global"),
            ("system-update", "update.notice", None, None, "system", "system"),
        ):
            with self.subTest(identifier=identifier):
                event = {**self.event(), "id": identifier, "kind": kind, "project": source, "name": "portable"}
                if kind == APPROVAL_REQUIRED:
                    event.update(pending_entities=["fixture"], project_approval=True, approval_subject=subject)
                self.queue.publish(event)
                item = next(item for item in self.queue.sync("desktop")["items"] if item["id"] == identifier)
                self.assertEqual((item["kind"], item["notification_scope"], item["scope"]), (kind, display, access))
                detail = self.queue.detail(identifier, access)
                self.assertEqual((detail["notification_scope"], detail.get("approval_subject")), (display, subject))

    def test_share_presentation_separates_waiting_blocked_and_published(self):
        from hermes_codex_learning.notice_presentation import notice_presentation

        for kind, reason, title, tone in (
            ("publication.pending", "executable_resource_needs_validation", "候选待检查", "waiting"),
            ("publication.pending", "host_validation_failed", "共享检查受阻", "warning"),
            ("publication.applied", None, "技能已共享", "success"),
        ):
            with self.subTest(title=title):
                result = notice_presentation({"kind": kind, "name": "portable", "publication_reason": reason})
                self.assertEqual((result["title"], result["body"], result["tone"]), (title, "portable", tone))

        missing = notice_presentation({
            "kind": "publication.pending",
            "name": "remote-service-cutovers",
            "publication_reason": "missing_applicability_limits_or_verification",
            "missing_sections": ["limits"],
        })
        self.assertEqual(missing["title"], "共享检查受阻")
        self.assertIn("限制/边界", missing["sections"][0]["text"])

    def test_persisted_legacy_receipt_remains_readable_and_unchanged(self):
        event = self.event()
        path = persist_receipt(self.queue.state, event)
        original = b'{"summary":"old","text":"historical","scope":"/work/a"}'
        path.write_bytes(original)
        self.assertEqual(persist_receipt(self.queue.state, event).read_bytes(), original)

    def test_macos_dispatch_uses_the_scope_sender_and_keeps_compatibility_fallback(self):
        project = self.root / "Hermes 学习.app"
        shared = global_notifier_app(project)
        for app, identifier in ((project, APP_ID), (shared, GLOBAL_APP_ID)):
            binary = app / "Contents/MacOS" / APP_EXECUTABLE
            binary.parent.mkdir(parents=True)
            binary.touch()
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
        event = {**self.event(), "id": "scope-project"}
        publication = {**event, "id": "scope-publication", "kind": "approval.required",
                       "approval_subject": "publication", "project_approval": True}
        with patch("hermes_codex_learning.notify.notifier_app", return_value=project), \
             patch("hermes_codex_learning.notify.subprocess.run", return_value=SimpleNamespace(returncode=0)) as post:
            self.assertEqual(macos_notification(self.queue.state, event)["transport"], APP_ID)
            self.assertEqual(macos_notification(self.queue.state, publication)["transport"], GLOBAL_APP_ID)
            self.assertEqual(Path(post.call_args.args[0][0]).parents[2], shared)
            legacy_id = "com.cy.hermes-learning-global-receipts"
            (shared / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": legacy_id}))
            self.assertEqual(macos_notification(self.queue.state, publication)["transport"], legacy_id)
            (shared / "Contents/MacOS" / APP_EXECUTABLE).unlink()
            self.assertEqual(macos_notification(self.queue.state, publication)["transport"], APP_ID)
            self.assertEqual(post.call_count, 4)

    def test_success_without_changes_is_silent(self):
        event = self.event()
        event["applied"] = []
        self.queue.publish(event)
        self.assertEqual(self.queue.poll("desktop"), [])
