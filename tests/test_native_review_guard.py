"""The patched direct review entry must preserve native non-interactive capture."""
import contextlib
import contextvars
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class NativeReviewGuardTests(unittest.TestCase):
    def exercise(self, *, tag="v0.23.0", fail=False, enabled=True, intent=None):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "patches" / tag / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        text = (manifest_path.parent / manifest.get("patch_file", "native-learning-api-v1.patch")).read_text()
        section = re.search(r"(?ms)^diff --git a/agent/learning_runtime.py.*?(?=^diff --git|\Z)", text).group()
        source = "\n".join(line[1:] for line in section.splitlines() if line.startswith("+") and not line.startswith("+++"))
        namespace = {"__name__": "acceptance_learning_runtime"}
        exec(compile(source, "learning_runtime.py", "exec"), namespace)
        def interactive(*args):
            raise AssertionError("An unattended review attempted interactive secret capture")
        capture = contextvars.ContextVar("capture", default=interactive)
        @contextlib.contextmanager
        def bind(callback):
            token = capture.set(callback)
            try:
                yield
            finally:
                capture.reset(token)
        seen = []
        def fork(*args):
            self.fork_details = {"prompt":args[2], "attended":args[-1]}
            seen.append(capture.get()("FIXTURE_KEY", "synthetic request"))
            if fail:
                raise RuntimeError("fixture failure")
        review = types.SimpleNamespace(
            load_background_review_settings=lambda: (enabled, {}), _parent_can_emit_tool_calls=lambda _: True,
            _ReviewForkState=lambda: types.SimpleNamespace(review_agent=None),
            _PROMPT_NAME_BY_SCOPE={(True, True): "prompt"}, prepare_background_review_run=lambda _: {},
            _set_thread_approval_callback=lambda _: None, _bg_review_auto_deny=lambda *_: False,
            _bg_review_skip_secret=lambda *_: None, _run_review_fork=fork,
            finish_background_review_run=lambda *_: None)
        agent = types.ModuleType("agent")
        agent.background_review = review
        modules = {"agent": agent, "run_agent": types.SimpleNamespace(_review_should_defer=lambda *_: False),
                   "agent.learning_results": types.SimpleNamespace(review_outcome=lambda st, error, **_: {"run_status": "failed" if error else "completed"}),
                   "agent.thread_scoped_output": types.SimpleNamespace(thread_scoped_silence=contextlib.nullcontext),
                   "tools": types.ModuleType("tools"), "tools.skills_tool": types.SimpleNamespace(bind_turn_secret_capture=bind)}
        runtime = namespace["NativeLearningRuntime"]()
        runtime.agent = lambda *_: types.SimpleNamespace(prompt="fixture")
        with patch.dict(sys.modules, modules), patch.object(namespace["logger"], "exception"):
            result = runtime.review("fixture", {}, [], intent or {"memory": True, "skills": True})
        return result, seen, capture.get() is interactive

    def test_unattended_direct_review_never_uses_interactive_secret_capture(self):
        for tag in ("v0.23.0", "v0.23.1"):
            with self.subTest(tag=tag):
                result, seen, restored = self.exercise(tag=tag)
                self.assertEqual(result["run_status"], "completed")
                self.assertEqual(seen, [None])
                self.assertTrue(restored)

    def test_capture_context_is_restored_when_review_fails(self):
        for tag in ("v0.23.0", "v0.23.1"):
            with self.subTest(tag=tag):
                result, seen, restored = self.exercise(tag=tag, fail=True)
                self.assertEqual(result["run_status"], "failed")
                self.assertEqual(seen, [None])
                self.assertTrue(restored)

    def test_recovery_checks_prior_writes_in_the_same_unattended_fork(self):
        for tag in ("v0.23.0", "v0.23.1"):
            with self.subTest(tag=tag):
                result, seen, restored = self.exercise(tag=tag, intent={"memory":True, "skills":True,
                    "recovery":{"applied":[{"name":"learned-existing"}], "staged":[{"pending_id":"pending-write"}]}})
                self.assertEqual(result["run_status"], "completed")
                self.assertIn("learned-existing", self.fork_details["prompt"])
                self.assertIn("pending-write", self.fork_details["prompt"])
                self.assertFalse(self.fork_details["attended"])
                self.assertEqual(seen, [None])
                self.assertTrue(restored)

    def test_manual_focus_runs_when_automatic_reviews_are_disabled_without_elevating_writes(self):
        for tag in ("v0.23.0", "v0.23.1"):
            with self.subTest(tag=tag):
                result, seen, restored = self.exercise(tag=tag, enabled=False, intent={"memory":True,"skills":True,
                    "reason":"user_request","focus":"Only retain the verified retry rule","attended":False})
                self.assertEqual(result["run_status"], "completed")
                self.assertIn("Only retain the verified retry rule", self.fork_details["prompt"])
                self.assertFalse(self.fork_details["attended"])
                self.assertEqual(seen, [None])
                self.assertTrue(restored)
                result, seen, _ = self.exercise(tag=tag, enabled=False)
                self.assertEqual(result["reason"], "native_review_disabled")
                self.assertEqual(seen, [])
