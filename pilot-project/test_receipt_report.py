"""Business fixture for real App execution; these fixtures are not learning evidence."""
import unittest
from unittest.mock import patch

from receipt_report import describe_receipt, render_markdown


class ReceiptReportTests(unittest.TestCase):
    def test_synthetic_model_probe_does_not_prove_app_capture(self):
        result = describe_receipt({"input_provenance": "synthetic_acceptance_fixture_not_app_hooks",
                                   "run_status": "completed", "applied_operations": 4, "api_calls": 6})
        self.assertEqual(result["execution_evidence"], "synthetic")
        self.assertFalse(result["app_capture_verified"])
        self.assertEqual(result["learning_status"], "completed")
        self.assertEqual(result["applied_operations"], 4)

    def test_real_app_capture_requires_host_identity_and_observed_hooks(self):
        result = describe_receipt({"input_provenance": "real_codex_app_hooks", "session_id": "chat-a",
                                   "turn_id": "turn-a", "hook_events": ["UserPromptSubmit", "PostToolUse", "Stop"]})
        self.assertTrue(result["app_capture_verified"])
        self.assertEqual(result["execution_evidence"], "app")
        for field in ("session_id", "turn_id", "hook_events"):
            with self.subTest(field=field):
                source = {"input_provenance": "real_codex_app_hooks", "session_id": "chat-a",
                          "turn_id": "turn-a", "hook_events": ["UserPromptSubmit", "PostToolUse", "Stop"]}
                del source[field]
                self.assertFalse(describe_receipt(source)["app_capture_verified"])

    def test_missing_usage_and_fee_remain_unknown(self):
        result = describe_receipt({})
        self.assertIsNone(result["api_calls"])
        self.assertIsNone(result["actual_fee_usd"])
        self.assertIsNone(result["learning_status"])

    def test_estimated_zero_is_not_actual_zero(self):
        result = describe_receipt({"api_calls": 6, "estimated_cost_usd": 0.0})
        self.assertEqual(result["api_calls"], 6)
        self.assertIsNone(result["actual_fee_usd"])

    def test_skill_delivery_adoption_and_validation_are_separate(self):
        result = describe_receipt({"skill_loaded": True, "skill_version": "v2"})
        self.assertEqual(result["skill_stage"], "loaded")
        self.assertFalse(result["skill_validated"])
        self.assertEqual(describe_receipt({"skill_loaded": True, "skill_adopted": True})["skill_stage"], "adopted")
        self.assertTrue(describe_receipt({"skill_loaded": True, "skill_adopted": True,
                                          "skill_validation_passed": True})["skill_validated"])

    def test_partial_failure_keeps_successful_writes(self):
        result = describe_receipt({"run_status": "failed", "applied_operations": 1})
        self.assertEqual(result["learning_status"], "failed")
        self.assertEqual(result["applied_operations"], 1)


class RenderMarkdownTests(unittest.TestCase):
    def test_synthetic_sample_stays_synthetic_after_successful_learning(self):
        receipt = {
            "input_provenance": "synthetic_acceptance_fixture_not_app_hooks",
            "session_id": "sample-chat",
            "turn_id": "sample-turn",
            "hook_events": ["UserPromptSubmit", "PostToolUse", "Stop"],
            "run_status": "completed",
            "applied_operations": 4,
            "api_calls": 6,
            "estimated_cost_usd": 0.0,
        }
        self.assertEqual(
            render_markdown(receipt),
            "- 采集证据：合成样例（不能证明 App 采集）\n"
            "- 复盘状态：已完成\n"
            "- 已生效变更：4 项\n"
            "- 技能阶段：未知\n"
            "- API 调用次数：6 次\n"
            "- 实际费用：未知",
        )

    def test_complete_app_identity_sample_with_validated_skill_and_actual_fee(self):
        receipt = {
            "input_provenance": "real_codex_app_hooks",
            "session_id": "sample-chat",
            "turn_id": "sample-turn",
            "hook_events": ["UserPromptSubmit", "PostToolUse", "Stop"],
            "run_status": "completed",
            "applied_operations": 2,
            "skill_loaded": True,
            "skill_adopted": True,
            "skill_validation_passed": True,
            "api_calls": 3,
            "actual_fee_usd": 0.0123,
            "estimated_cost_usd": 99,
        }
        self.assertEqual(
            render_markdown(receipt),
            "- 采集证据：Codex App（采集已核验）\n"
            "- 复盘状态：已完成\n"
            "- 已生效变更：2 项\n"
            "- 技能阶段：已采用（已验证）\n"
            "- API 调用次数：3 次\n"
            "- 实际费用：0.0123 USD",
        )

    def test_incomplete_app_samples_are_not_reported_as_verified(self):
        source = {
            "input_provenance": "real_codex_app_hooks",
            "session_id": "sample-chat",
            "turn_id": "sample-turn",
            "hook_events": ["UserPromptSubmit", "PostToolUse", "Stop"],
        }
        for field in ("session_id", "turn_id", "hook_events"):
            with self.subTest(missing_field=field):
                receipt = dict(source)
                del receipt[field]
                self.assertIn(
                    "- 采集证据：Codex App 来源（采集未核验）",
                    render_markdown(receipt).splitlines(),
                )
        for event in source["hook_events"]:
            with self.subTest(missing_hook=event):
                receipt = dict(source, hook_events=[hook for hook in source["hook_events"] if hook != event])
                self.assertIn(
                    "- 采集证据：Codex App 来源（采集未核验）",
                    render_markdown(receipt).splitlines(),
                )

    def test_partial_failure_preserves_applied_changes_and_usage(self):
        lines = render_markdown({
            "run_status": "failed", "applied_operations": 1, "api_calls": 2,
            "skill_loaded": True,
        }).splitlines()
        self.assertIn("- 复盘状态：失败", lines)
        self.assertIn("- 已生效变更：1 项", lines)
        self.assertIn("- 技能阶段：已加载", lines)
        self.assertIn("- API 调用次数：2 次", lines)
        self.assertIn("- 实际费用：未知", lines)

    def test_missing_and_null_values_are_unknown(self):
        for receipt in ({}, {
            "input_provenance": None,
            "run_status": None,
            "applied_operations": None,
            "api_calls": None,
            "actual_fee_usd": None,
        }):
            with self.subTest(receipt=receipt):
                self.assertEqual(
                    render_markdown(receipt),
                    "- 采集证据：未知\n"
                    "- 复盘状态：未知\n"
                    "- 已生效变更：未知\n"
                    "- 技能阶段：未知\n"
                    "- API 调用次数：未知\n"
                    "- 实际费用：未知",
                )

    def test_estimates_never_replace_unknown_actual_fee(self):
        for estimate in (0.0, 12.5):
            for actual in ({}, {"actual_fee_usd": None}):
                with self.subTest(estimate=estimate, actual=actual):
                    receipt = dict(actual, estimated_cost_usd=estimate, api_calls=6)
                    lines = render_markdown(receipt).splitlines()
                    self.assertIn("- 实际费用：未知", lines)
                    self.assertIn("- API 调用次数：6 次", lines)

    def test_explicit_zero_values_are_preserved(self):
        lines = render_markdown({
            "applied_operations": 0, "api_calls": 0, "actual_fee_usd": 0.0,
            "estimated_cost_usd": 12.5,
        }).splitlines()
        self.assertIn("- 已生效变更：0 项", lines)
        self.assertIn("- API 调用次数：0 次", lines)
        self.assertIn("- 实际费用：0.0 USD", lines)

    def test_skill_loading_adoption_and_validation_remain_distinct(self):
        cases = (
            ({"skill_version": "v2", "skill_validation_passed": True}, "未知"),
            ({"skill_loaded": True, "skill_validation_passed": True}, "已加载"),
            ({"skill_loaded": True, "skill_adopted": True}, "已采用"),
            ({"skill_loaded": True, "skill_adopted": True,
              "skill_validation_passed": True}, "已采用（已验证）"),
        )
        for receipt, expected in cases:
            with self.subTest(receipt=receipt):
                self.assertIn(f"- 技能阶段：{expected}", render_markdown(receipt).splitlines())

    def test_renderer_uses_describe_receipt_without_mutating_input(self):
        receipt = {"run_status": "failed", "applied_operations": 1}
        original = dict(receipt)
        with patch("receipt_report.describe_receipt", wraps=describe_receipt) as describe:
            render_markdown(receipt)
        describe.assert_called_once_with(receipt)
        self.assertEqual(receipt, original)


if __name__ == "__main__":
    unittest.main()
