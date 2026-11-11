import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.notify import (LEGACY_APP_ID, APP_EXECUTABLE, legacy_notifier_app,
                                         macos_notification, message)
from hermes_codex_learning.review_input import BUDGET_EXHAUSTED
from hermes_codex_learning.review_receipt import ReviewWrites, receipt_text


class ReviewReceiptTests(unittest.TestCase):
    def test_dispatch_uses_preserved_legacy_app_during_interrupted_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            app = state / "Hermes 学习.app"
            legacy = legacy_notifier_app(app)
            (legacy / "Contents/MacOS").mkdir(parents=True)
            (legacy / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": LEGACY_APP_ID}))
            with patch("hermes_codex_learning.notify.notifier_app", return_value=app), \
                 patch("hermes_codex_learning.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                result = macos_notification(state, self.event("fallback"))
            self.assertEqual(result["transport"], LEGACY_APP_ID)
            self.assertEqual(run.call_args.args[0][0], str(legacy / "Contents/MacOS" / APP_EXECUTABLE))

    def event(self, identity="review-a", **fields):
        event = {"id": identity, "kind": "review.finished", "run_status": "completed", **fields}
        return {**event, "message": message(event)}

    def test_staged_memory_receipt_shows_the_review_identifier_and_proposed_change(self):
        calls = {("memory", "staged-call"): {"operations": [
            {"action": "patch", "old_text": "previous project rule", "content": "proposed project rule"}]}}
        event = self.event(staged=[{"tool": "memory", "call_id": "staged-call", "result": {
            "staged": True, "pending_id": "11111111", "message": "Background consolidation needs approval"}}])
        text = receipt_text(event, calls, 0)
        self.assertIn("修订 · 记忆", text)
        self.assertIn("待审编号：11111111", text)
        self.assertIn("previous project rule", text)
        self.assertIn("拟写入的内容\nproposed project rule", text)
        self.assertIn("尚未生效", text)

    def test_refused_batch_receipt_names_operations_and_shows_the_native_reason(self):
        calls = {("skill_manage", "refused-call"): {"operations": [
            {"action": "patch", "name": "learned-checks", "file_path": "references/check.md"},
            {"action": "write_file", "name": "learned-checks", "file_path": "scripts/check.py"}]}}
        event = self.event(refused=[{"tool": "skill_manage", "call_id": "refused-call", "result": {
            "error": "[review_resource_path] Resource is outside the permitted package"}}])
        text = receipt_text(event, calls, 0)
        self.assertIn("修订 · 技能 learned-checks · references/check.md", text)
        self.assertIn("写入文件 · 技能 learned-checks · scripts/check.py", text)
        self.assertIn("拒绝原因：[review_resource_path] Resource is outside the permitted package", text)
        self.assertNotIn("未命名技能", text)

    def test_background_failure_receipt_does_not_tell_the_user_to_continue_a_chat(self):
        upstream_error = (
            "⚠️ No reply: the turn stopped while a tool result was still pending and the model produced "
            "no follow-up text. Send `continue` to let it summarize."
        )
        ordinary = self.event(run_status="failed", error=upstream_error)
        exhausted = self.event(run_status="failed", error=upstream_error, failure_reason=BUDGET_EXHAUSTED)

        self.assertIn("收到工具结果后结束", receipt_text(ordinary, {}, 0))
        self.assertIn("输入超出预算", exhausted["message"])
        self.assertIn("达到输入预算", receipt_text(exhausted, {}, 0))
        for event in (ordinary, exhausted):
            self.assertNotIn("continue", receipt_text(event, {}, 0))
            self.assertEqual(event["error"], upstream_error)

    def test_receipt_joins_applied_calls_and_ordered_batch_operations(self):
        writes = ReviewWrites()
        skill_op = {"name": "validation", "action": "patch", "file_path": "references/check.md"}
        native_ops = [{**skill_op, "success": True}, {**skill_op, "success": True}]
        facts = [{"tool": "memory", "call_id": "m", "result": {"target": "memory"}}]
        facts.extend({"tool": "skill_manage", "call_id": "s", "operation": op,
                      "result": {"results": native_ops}} for op in native_ops)
        with writes.capture("native-handle") as calls:
            def observe(tool, call_id, args, handle="native-handle"):
                writes.record(session_id=handle, tool_name=tool, tool_call_id=call_id, args=args)

            observe("memory", "m", {"operations": [{"action": "add", "content": "Use frozen inputs"}]})
            observe("memory", "m", {"action": "add", "content": "Another session"}, "other-handle")
            observe("memory", "failed", {"action": "add", "content": "Failed content"})
            observe("skill_manage", "s", {"operations": [
                {**skill_op, "old_string": "Original rule", "new_string": "Revised rule"},
                {**skill_op, "old_string": "Revised rule", "new_string": "Use fixed input hashes"},
            ]})
        writes.record(session_id="native-handle", tool_name="memory", tool_call_id="m",
                      args={"action": "add", "content": "Late write"})
        receipt = receipt_text(self.event(applied=facts), calls, 0)
        self.assertIn("2 项变更生效", receipt)
        self.assertIn("新增 · 记忆\n写入的内容\nUse frozen inputs", receipt)
        self.assertIn("匹配的原文\nOriginal rule\n写入的内容\nRevised rule", receipt)
        self.assertIn("匹配的原文\nRevised rule\n写入的内容\nUse fixed input hashes", receipt)
        for text in ("Another session", "Failed content", "Late write"):
            self.assertNotIn(text, receipt)

    def test_same_summary_has_separate_receipts_and_retries_keep_the_original(self):
        with tempfile.TemporaryDirectory() as directory, patch("hermes_codex_learning.notify.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            state = Path(directory)
            a = macos_notification(state, self.event("a", receipt="A detail"))
            b = macos_notification(state, self.event("b", receipt="B detail"))
            retry = macos_notification(state, self.event("a", receipt="New text"))
            self.assertEqual(a, retry)
            self.assertTrue(a["dispatched"])
            self.assertNotEqual(a["receipt_path"], b["receipt_path"])
            path = Path(a["receipt_path"])
            self.assertEqual(json.loads(path.read_text())["text"], "A detail")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(run.call_args.args[0][1:], ["--post", str(path)])


if __name__ == "__main__":
    unittest.main()
