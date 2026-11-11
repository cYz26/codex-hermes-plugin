import json
import plistlib
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
                writes.record(session_id=handle, tool_name=tool, tool_call_id=call_id, args=args,
                              result={"success": True})

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

    def test_refused_batch_attempts_keep_names_and_reasons_without_proposed_text(self):
        writes = ReviewWrites()
        operation = {"name": "learned-local", "action": "patch", "file_path": "references/check.md",
                     "old_string": "UNAPPLIED_OLD_TEXT", "new_string": "UNAPPLIED_NEW_TEXT"}
        reason = "技能不在本次复盘可修改范围内。"
        facts = []
        with writes.capture("native-handle") as calls:
            for index in range(3):
                call_id = f"refused-{index}"
                self.assertIsNone(writes.record_attempt(
                    session_id="native-handle", tool_name="skill_manage", tool_call_id=call_id,
                    args={"operations": [operation]}))
                writes.record(session_id="native-handle", tool_name="skill_manage", tool_call_id=call_id,
                              args={"operations": [operation]}, status="blocked",
                              result=json.dumps({"error": reason}))
                writes.record_attempt(session_id="other-handle", tool_name="skill_manage", tool_call_id=call_id,
                                      args={"action": "patch", "name": "OTHER_SESSION_SKILL"})
                facts.append({"tool": "skill_manage", "call_id": call_id, "action": None, "name": None,
                              "result": {"error": reason}})
        summary = "本次校验拒绝了范围外的技能修订，现有记录不足以证明变更生效。"
        receipt = receipt_text(self.event(refused=facts, review_summary=summary), calls, 0)
        self.assertIn("复盘简述\n" + summary, receipt)
        self.assertIn("写入：0 项变更已生效", receipt)
        self.assertIn("被拒绝：3 次调用", receipt)
        self.assertLess(receipt.index("状态简述"), receipt.index("原因："))
        self.assertIn("修订 · 技能 learned-local · references/check.md（3 次尝试）", receipt)
        self.assertEqual(receipt.count("原因：" + reason), 1)
        for text in ("未命名技能", "UNAPPLIED_OLD_TEXT", "UNAPPLIED_NEW_TEXT", "OTHER_SESSION_SKILL"):
            self.assertNotIn(text, receipt)
            self.assertNotIn(text, json.dumps(list(calls.values())))

    def test_legacy_refusal_reports_missing_name_and_available_error(self):
        fact = {"tool": "skill_manage", "call_id": "legacy", "name": None,
                "result": {"error": "Use only admitted project memory and listed skills."}}
        receipt = receipt_text(self.event(refused=[fact]), {}, 0)
        self.assertIn("技能操作（名称未记录）", receipt)
        self.assertIn("原因：Use only admitted project memory and listed skills.", receipt)
        self.assertNotIn("未命名技能", receipt)
        self.assertNotIn("SKILL.md", receipt)

    def test_malformed_action_in_native_fact_still_produces_a_refusal_receipt(self):
        fact = {"tool": "skill_manage", "call_id": "malformed", "name": "learned-local", "action": ["patch"],
                "result": {"error": "action 参数必须是字符串。"}}
        receipt = receipt_text(self.event(refused=[fact]), {}, 0)
        self.assertIn("操作 · 技能 learned-local · SKILL.md", receipt)
        self.assertIn("原因：action 参数必须是字符串。", receipt)

    def test_attempt_metadata_and_refusal_errors_filter_credentials(self):
        token = ".".join(("eyJ" + "a" * 25, "b" * 25, "c" * 25))
        writes = ReviewWrites()
        with writes.capture("native-handle") as calls:
            writes.record_attempt(session_id="native-handle", tool_name="skill_manage", tool_call_id="blocked",
                                  args={"name": token, "action": "patch"})
        fact = {"tool": "skill_manage", "call_id": "blocked", "result": {"error": "credential: " + token}}
        receipt = receipt_text(self.event(refused=[fact]), calls, 0)
        self.assertNotIn(token, json.dumps(list(calls.values())))
        self.assertNotIn(token, receipt)
        self.assertIn("[REDACTED TOKEN]", receipt)

    def test_summary_distinguishes_successful_changes_from_refused_attempts(self):
        event = self.event(applied_entities=["skill:learned-local"], refused=[{}, {}, {}])
        self.assertEqual(event["message"], "Hermes 已学习：1 项变更生效；3 次操作尝试被拒绝")

    def test_plugin_observes_blocked_calls_before_execution_and_disposes_hooks(self):
        plugin = runpy.run_path(str(Path(__file__).resolve().parents[1] / "hermes-plugin/__init__.py"))
        hooks, registrations = {}, []

        def register_hook(name, callback):
            hooks[name] = callback
            registration = SimpleNamespace(dispose=Mock())
            registrations.append(registration)
            return registration

        api = SimpleNamespace(close=Mock())
        ctx = SimpleNamespace(register_system_prompt_section=Mock(), register_cli_command=Mock(),
                              register_hook=register_hook, learning=Mock(return_value=api))
        plugin["register"](ctx)
        serve = ctx.register_cli_command.call_args.kwargs["handler_fn"]

        def run(settings, native_api, writes):
            self.assertIn("pre_tool_call", hooks)
            with writes.capture("native-handle") as calls:
                self.assertIsNone(hooks["pre_tool_call"](
                    session_id="native-handle", tool_name="skill_manage", tool_call_id="blocked",
                    args={"name": "learned-local", "action": "patch", "content": "UNAPPLIED_CONTENT"}))
            fact = {"tool": "skill_manage", "call_id": "blocked", "result": {"error": "scope refusal"}}
            return receipt_text(self.event(refused=[fact]), calls, 0)

        with patch("hermes_codex_learning.config.Settings.load", return_value=SimpleNamespace()), \
             patch("hermes_codex_learning.daemon.run", side_effect=run), \
             patch("hermes_codex_learning.review_input.install_review_policy"), \
             patch("hermes_codex_learning.workflow_review.install_workflow_policy", return_value=None), \
             patch("hermes_codex_learning.semantic_review.install_semantic_policy", return_value=None):
            receipt = serve(SimpleNamespace(settings="synthetic-settings"))
        self.assertIn("修订 · 技能 learned-local · SKILL.md", receipt)
        self.assertIn("原因：scope refusal", receipt)
        self.assertNotIn("UNAPPLIED_CONTENT", receipt)
        for registration in registrations:
            registration.dispose.assert_called_once_with()

    def test_review_summary_and_actual_status_precede_the_changes(self):
        summary = "参数变更导致旧测试失配。依据回归结果，修改接口时应先检查调用方与 mock；适用于接口维护。"
        fact = {"tool": "skill_manage", "call_id": "skill", "name": "interface-checks", "action": "patch"}
        calls = {("skill_manage", "skill"): {"action": "patch", "name": "interface-checks",
                  "old_string": "旧规则", "new_string": "新规则"}}
        event = self.event(review_summary=summary, applied=[fact])

        receipt = receipt_text(event, calls, 0)

        self.assertIn("复盘简述\n" + summary, receipt)
        self.assertIn("状态简述", receipt)
        self.assertIn("复盘：完成", receipt)
        self.assertIn("写入：1 项变更已生效", receipt)
        self.assertIn("技能共享：尚未确认", receipt)
        self.assertIn("后续任务验证：尚未确认", receipt)
        self.assertLess(receipt.index("复盘简述"), receipt.index("匹配的原文"))
        self.assertLess(receipt.index("状态简述"), receipt.index("匹配的原文"))
        self.assertIn("匹配的原文\n旧规则\n写入的内容\n新规则", receipt)

    def test_partial_review_reports_applied_pending_refused_and_uncertain_results(self):
        applied = {"tool": "skill_manage", "name": "written", "action": "create"}
        staged = {"tool": "memory", "action": "patch", "result": {"target": "memory"}}
        event = self.event(run_status="failed", review_summary=None, applied=[applied], staged=[staged],
                           refused=[{"tool": "skill_manage", "name": "locked"}], uncertain=["unknown write"])

        receipt = receipt_text(event, {}, 0)

        self.assertIn("本次复盘未生成简述。", receipt)
        self.assertIn("复盘：失败", receipt)
        self.assertIn("写入：1 项变更已生效", receipt)
        self.assertIn("待批准：1 组修改", receipt)
        self.assertIn("被拒绝：1 次调用", receipt)
        self.assertIn("结果待确认：1 项", receipt)
        self.assertIn("后续任务验证：尚未确认", receipt)
        self.assertNotIn("复盘：完成", receipt)

    def test_old_results_and_malformed_summaries_remain_readable(self):
        for fields in ({}, {"review_summary": {}}, {"review_summary": "   "}):
            with self.subTest(fields=fields):
                receipt = receipt_text(self.event(**fields), {}, 0)
                self.assertIn("复盘简述", receipt)
                self.assertIn("复盘：完成", receipt)
                self.assertIn("此记录未包含" if not fields else "本次复盘未生成", receipt)

    def test_banner_includes_a_short_explanation_after_the_actual_status(self):
        summary = "核对调用方与回归证据。" * 30
        event = self.event(review_summary=summary, applied=[{"tool": "skill_manage", "name": "checks"}])
        status, preview = event["message"].split("\n", 1)
        self.assertIn("1 项变更生效", status)
        self.assertTrue(preview.startswith("核对调用方与回归证据。"))
        self.assertLessEqual(len(preview), 100)

    def test_a_publication_receipt_does_not_claim_an_unknown_review_status(self):
        event = {"kind": "publication.applied", "name": "shared-checks"}
        receipt = receipt_text({**event, "message": message(event)}, {}, 0)
        self.assertIn("已发布 1 项通用技能", receipt)
        self.assertNotIn("复盘简述", receipt)
        self.assertNotIn("复盘：结果待确认", receipt)

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
