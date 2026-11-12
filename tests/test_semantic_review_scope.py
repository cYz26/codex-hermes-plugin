"""Selected review writes must match the tools and prompt sent to the fork."""
import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.semantic_review import install_semantic_policy


class SemanticReviewScopeTests(unittest.TestCase):
    def exercise(self, intents, *, fail=False):
        parent = SimpleNamespace(
            session_id="scope", _memory_store=SimpleNamespace(), _memory_manager=None,
            _memory_enabled=True,
            tools=[{"function": {"name": name}} for name in
                   ("memory", "skill_manage", "skill_view", "terminal")],
        )
        background = SimpleNamespace(build_cache_parity_fork=lambda agent, **kwargs:
                                     (copy.copy(agent), {}, False))
        original_fork = background.build_cache_parity_fork
        observed, hooks = [], {}
        runtime = SimpleNamespace(agent=lambda *args: parent)
        request = "Synthetic CSV header matching requires normalization."
        answer = "Synthetic CSV header normalization follows a repeatable procedure."
        messages = [{"role":"user", "content":request, "evidence_ref":"T1.request"},
                    {"role":"assistant", "content":answer, "evidence_ref":"T1.result"}]
        def proof(classification):
            ref, quote = ("T1.result", answer) if classification == "procedure" else ("T1.request", request)
            return {"classification":classification, "basis":[{"ref":ref, "quote":quote}],
                    "rationale":"CSV header matching uses normalization; retain the supported synthetic rule."}

        def native_review(handle, source, messages, intent):
            fork = background.build_cache_parity_fork(runtime.agent(handle, source))[0]
            observed.append({
                "tools": fork.valid_tool_names,
                "prompt": fork._build_system_prompt(),
                "memory_enabled": fork._memory_enabled,
                "memory_guard": hooks["pre_tool_call"](
                    session_id=handle, tool_name="memory",
                    args={"action": "add", "target": "memory", "content":request,
                          "evidence":proof("project_fact")}),
                "skills_guard": hooks["pre_tool_call"](
                    session_id=handle, tool_name="skill_manage",
                    args={"action": "create", "name": "learned-synthetic", "content":answer,
                          "evidence":proof("procedure")}),
            })
            if fail:
                raise RuntimeError("synthetic review interruption")
            return {"run_status": "completed"}

        runtime.review = native_review
        api = SimpleNamespace(_service=SimpleNamespace(runtime=runtime),
                              read=lambda **kwargs: {"skills": [], "memories": {}})

        def register(name, callback):
            hooks[name] = callback
            return SimpleNamespace(dispose=lambda: hooks.pop(name, None))

        agent_module = types.ModuleType("agent")
        agent_module.background_review = background
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root, root, Path(sys.executable),
                                data_policy={"enabled": True})
            registration = install_semantic_policy(api, settings, SimpleNamespace(register_hook=register))
            try:
                with patch.dict(sys.modules, {"agent": agent_module}):
                    for intent in intents:
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "synthetic review interruption"):
                                runtime.review("scope", {"executor": "codex"}, messages, intent)
                        else:
                            result = runtime.review("scope", {"executor": "codex"}, messages, intent)
                            self.assertEqual(result["run_status"], "completed")
                            expected = ({"memory"} if intent["memory"] else set()) | ({"skill_manage"} if intent["skills"] else set())
                            self.assertEqual({claim["tool"] for claim in result["learning_candidates"] if claim["state"]=="accepted"}, expected)
                        self.assertIs(background.build_cache_parity_fork, original_fork)
                        self.assertEqual(parent.valid_tool_names, {"memory", "skill_manage", "skill_view"})
                        self.assertTrue(parent._memory_enabled)
            finally:
                registration.dispose()
        return observed

    def test_skills_review_advertises_only_skill_writes(self):
        result = self.exercise([{"memory": False, "skills": True}])[0]
        self.assertEqual(result["tools"], {"skill_manage", "skill_view"})
        self.assertNotIn("Use native memory to add", result["prompt"])
        self.assertIn("skill_manage", result["prompt"])
        self.assertFalse(result["memory_enabled"])
        self.assertEqual(result["memory_guard"]["action"], "block")
        self.assertEqual(result["skills_guard"]["action"], "modify")

    def test_memory_review_does_not_advertise_skill_writes(self):
        result = self.exercise([{"memory": True, "skills": False}])[0]
        self.assertEqual(result["tools"], {"memory", "skill_view"})
        self.assertIn("Use native memory to add", result["prompt"])
        self.assertNotIn("skill_manage", result["prompt"])
        self.assertTrue(result["memory_enabled"])
        self.assertEqual(result["memory_guard"]["action"], "modify")
        self.assertEqual(result["skills_guard"]["action"], "block")

    def test_scope_changes_preserve_future_memory_and_skill_reviews(self):
        results = self.exercise([{"memory": False, "skills": True},
                                 {"memory": True, "skills": False},
                                 {"memory": True, "skills": True}])
        self.assertEqual([result["tools"] for result in results],
                         [{"skill_manage", "skill_view"}, {"memory", "skill_view"},
                          {"memory", "skill_manage", "skill_view"}])
        self.assertEqual(results[-1]["memory_guard"]["action"], "modify")
        self.assertEqual(results[-1]["skills_guard"]["action"], "modify")

    def test_interrupted_review_restores_the_native_fork_builder(self):
        self.exercise([{"memory": False, "skills": True}], fail=True)
