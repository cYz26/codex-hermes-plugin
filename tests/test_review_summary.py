import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.review_input import install_review_policy
from hermes_codex_learning.review_summary import MAX_SUMMARY_CHARS, capture_review_summary, summary_text


class ReviewSummaryTests(unittest.TestCase):
    def exercise(self, native_result, *, fail=False, foreign=False):
        prompts = []
        outcome = {"run_status": "failed" if fail else "completed", "applied": [], "staged": [],
                   "uncertain": [], "usage": {"api_calls": 2}}

        def fork(parent, messages, prompt, task, run, state, memory=False, explicit=False):
            prompts.append(prompt)
            state.result = native_result
            if fail:
                raise RuntimeError("Interrupted after a native write")

        background = SimpleNamespace(_review_input_token_budget=lambda *args: None,
                                     load_background_review_settings=lambda: (True, {}), _run_review_fork=fork)

        class Runtime:
            def agent(self, handle, source):
                return SimpleNamespace(session_id=handle, provider="fixture", model="fixture")

            def review(self, handle, source, messages, intent):
                state = SimpleNamespace(result=None)
                parent = self.agent("another-scope" if foreign else handle, source)
                try:
                    background._run_review_fork(parent, messages, "Native review prompt",
                                                {}, None, state, True, False)
                except RuntimeError:
                    pass  # Native runtime reports failure while preserving partial writes.
                return outcome

        runtime = Runtime()
        agent_module = types.ModuleType("agent")
        agent_module.background_review = background
        modules = {"agent": agent_module, "agent.background_review": background,
                   "agent.learning_runtime": SimpleNamespace(NativeLearningRuntime=Runtime)}
        api = SimpleNamespace(version=1, _service=SimpleNamespace(runtime=runtime))
        with patch.dict(sys.modules, modules):
            install_review_policy(api)
            with capture_review_summary(api, "scope-a") as captured:
                result = runtime.review("scope-a", {}, [{"role": "user", "content": "Review this correction."}],
                                        {"memory": True, "skills": True})
        self.assertIs(background._run_review_fork, fork)
        self.assertNotIn("review_summary", outcome)
        self.assertNotIn("review_summary", result)
        self.assertIsNone(runtime._notice_review_summary.current)
        return result, prompts, captured["text"]

    def test_same_native_review_returns_its_final_explanation_and_restores_the_fork(self):
        summary = "回归测试暴露接口调用方失配。维护接口时先核对调用方；适用于接口修改。"
        result, prompts, captured = self.exercise({"completed": True, "final_response": summary,
                                                   "last_reasoning": "PRIVATE_REASONING"})

        self.assertEqual(captured, summary)
        self.assertEqual(result["usage"], {"api_calls": 2})
        self.assertEqual(len(prompts), 1)
        self.assertIn("2–3 sentences", prompts[0])
        self.assertIn("evidence", prompts[0])
        self.assertIn("applicability", prompts[0])
        self.assertNotIn("PRIVATE_REASONING", str(result))

    def test_failed_or_missing_final_response_does_not_reuse_history_or_runtime_errors(self):
        values = [None, {"completed": True, "messages": [{"role": "assistant", "content": "OLD_SUMMARY"}]},
                  {"failed": True, "final_response": "No reply: Send `continue` to summarize."},
                  {"interrupted": True, "final_response": "partial"}, {"final_response": {"text": "invalid"}}]
        for native_result in values:
            with self.subTest(native_result=native_result):
                result, prompts, captured = self.exercise(native_result, fail=True)
                self.assertIsNone(captured)
                self.assertEqual(result["run_status"], "failed")
                self.assertEqual(len(prompts), 1)

    def test_another_native_session_keeps_its_original_prompt_and_summary_private(self):
        result, prompts, captured = self.exercise({"final_response": "ANOTHER_SESSION_SUMMARY"}, foreign=True)
        self.assertIsNone(captured)
        self.assertEqual(prompts, ["Native review prompt"])

    def test_summary_redacts_credentials_before_clipping(self):
        key = "sk-" + "n" * 64
        summary = summary_text("说明" * (MAX_SUMMARY_CHARS // 2 - 2) + key + " Bearer synthetic-notice-key")
        self.assertNotIn(key, summary)
        self.assertNotIn("sk-", summary)
        self.assertNotIn("synthetic-notice-key", summary)
        self.assertLessEqual(len(summary), MAX_SUMMARY_CHARS)
        self.assertTrue(summary.endswith("…"))

    def test_summary_uses_the_receipts_credential_filter(self):
        token = ".".join(("eyJ" + "a" * 25, "b" * 25, "c" * 25))
        summary = summary_text("依据请求结果复盘。\nToken: " + token + "\nCookie: session=synthetic-secret")
        self.assertNotIn(token, summary)
        self.assertNotIn("synthetic-secret", summary)
        self.assertIn("[REDACTED TOKEN]", summary)


if __name__ == "__main__":
    unittest.main()
