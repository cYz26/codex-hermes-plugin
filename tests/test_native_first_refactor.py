import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook, model_context
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.enterprise import boundary, make_boundary
from hermes_codex_learning.knowledge import context_view
from hermes_codex_learning.learning_health import memory_capacity
from hermes_codex_learning.model_sync import (
    record_review_model_failure,
    switch_pilot_model,
    sync_with_codex,
)
from hermes_codex_learning.retention import run as run_retention
from hermes_codex_learning.semantic_review import SemanticGuard, install_semantic_policy
from hermes_codex_learning.shared_storage import _skill_filters
from hermes_codex_learning.store import Store, atomic_json, digest, dumps


class _FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn
        return name


class NativeFirstRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "pilot"
        self.project = self.root / "project"
        self.home.mkdir(parents=True)
        self.project.mkdir(parents=True)
        (self.home / "config.yaml").write_text(json.dumps({
            "_config_version": 1,
            "model": {"provider": "custom:ark", "default": "ark:gpt-6-2-sol", "api_mode": "codex_responses"},
            "agent": {"reasoning_effort": "medium"},
            "providers": {"ark": {"base_url": "http://127.0.0.1:17413/client-retry", "api_mode": "codex_responses"}},
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_review_protocol_preserves_native_tools_and_enforces_security_guards(self):
        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", checked_publication=True,
            review_protocol="native",
        )
        boundary_file = settings.state / "enterprise-boundary.json"
        settings.state.mkdir(parents=True, exist_ok=True)
        atomic_json(boundary_file, make_boundary(settings))
        settings = replace(settings, data_policy={
            "enabled": True, "mode": "trusted_enterprise", "enterprise_boundary": str(boundary_file),
        })
        store = Store(settings.state)
        self.addCleanup(store.close)
        store.set("scope:h1", {"project": str(self.project), "chat": "c1", "executor": "Codex App"})
        store.set("assessment:h1", {"allowed": True, "classification": "approved",
                                    "boundary_id": boundary(settings.data_policy)["id"]})

        fake_store = SimpleNamespace(user_profile_enabled=False)
        fake_agent = SimpleNamespace(
            session_id="h1",
            _memory_store=fake_store,
            _user_profile_enabled=False,
            ephemeral_system_prompt="base",
            _cached_system_prompt="native-system-prompt",
            tools=[{"function": {"name": "memory"}}, {"function": {"name": "skill_manage"}}],
        )
        seen = {}

        def orig_review(handle, source, messages, intent):
            seen["prompt"] = fake_agent._cached_system_prompt
            seen["tools"] = fake_agent.tools
            seen["user_enabled"] = fake_agent._memory_store.user_profile_enabled
            return {"run_status": "completed", "applied": [], "staged": [], "refused": [], "uncertain": []}

        runtime = SimpleNamespace(agent=lambda h, s: fake_agent, review=orig_review)
        api = SimpleNamespace(
            _service=SimpleNamespace(runtime=runtime),
            read=lambda kind="context", **kw: {"memories": {"MEMORY.md": "", "USER.md": ""}, "skills": []},
        )
        ctx = _FakeContext()
        with patch.dict(sys.modules, {"agent": SimpleNamespace(background_review=SimpleNamespace(build_cache_parity_fork=lambda *a, **k: None)),
                                      "agent.background_review": SimpleNamespace(build_cache_parity_fork=lambda *a, **k: None)}):
            install_semantic_policy(api, settings, ctx)
            res = runtime.review("h1", {"project": str(self.project), "executor": "Codex App"},
                                 [{"role": "user", "content": "hi"}], {"id": "i1", "memory": True, "skills": True})

        self.assertEqual(res["review_protocol"], "native")
        self.assertEqual(seen["prompt"], "native-system-prompt")
        self.assertTrue(seen["user_enabled"])
        self.assertEqual([t["function"]["name"] for t in seen["tools"]], ["memory", "skill_manage"])

        # Verify native-mode guard still allows native USER.md / skill_manage without evidence,
        # while blocking credentials and path traversal.
        guard = SemanticGuard(api, settings)
        guard.active["h1"] = {"mode": "native", "review_memory": True, "review_skills": True, "pending": {}, "memory": []}
        self.assertIsNone(guard.pre_tool(session_id="h1", tool_name="memory", args={"action": "add", "target": "user", "content": "Prefer concise output"}))
        self.assertIsNone(guard.pre_tool(session_id="h1", tool_name="skill_manage", args={"action": "create", "name": "learned-demo", "content": "---\nname: learned-demo\n---\nbody"}))
        synthetic_secret = "Bearer " + "sk-" + "proj-" + ("a" * 28)
        denied_secret = guard.pre_tool(session_id="h1", tool_name="memory", args={"action": "add", "target": "memory", "content": synthetic_secret})
        self.assertEqual(denied_secret["action"], "block")
        denied_escape = guard.pre_tool(session_id="h1", tool_name="skill_manage", args={"action": "write_file", "name": "learned-demo", "file_path": "../escape.md", "file_content": "x"})
        self.assertEqual(denied_escape["action"], "block")

    def test_stale_running_turns_do_not_block_curator_and_get_reaped(self):
        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", active_turn_timeout_seconds=600,
        )
        api = SimpleNamespace(read=lambda **kw: {})
        bridge = Bridge(settings, api)
        self.addCleanup(bridge.store.close)
        old_ts = time.time() - 3600
        turn_id = "turn-zombie"
        payload = {
            "source": {"host": "h", "project": str(self.project), "chat": "chat-zombie", "turn": turn_id},
            "user": "hello", "assistant": "", "tools": {}, "status": "running",
        }
        with bridge.store.db:
            bridge.store.db.execute(
                "INSERT INTO turns (id, payload, revision, observed_revision, stopped, activity) VALUES (?,?,?,?,?,?)",
                (turn_id, dumps(payload), 1, 0, None, old_ts),
            )
            bridge.store.db.execute(
                "INSERT INTO latest_turns (chat, turn_id, activity) VALUES (?,?,?)",
                ("chat-zombie", turn_id, old_ts),
            )
        self.assertFalse(bridge.has_active_turns())
        reaped = bridge.reap_stale_turns()
        self.assertEqual(reaped, 1)
        row = bridge.store.db.execute("SELECT stopped, payload FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertIsNotNone(row["stopped"])
        saved = json.loads(row["payload"])
        self.assertEqual(saved["status"], "incomplete")
        self.assertEqual(saved["_ended_by"], "stale_timeout")

    def test_model_sync_decoupled_effort_cap_and_same_protocol_fallback(self):
        codex_home = self.root / "codex"
        codex_home.mkdir()
        cat = codex_home / "ark-models.json"
        cat.write_text(json.dumps({
            "models": [
                {"slug": "ark:gpt-6-2-sol", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "max")]},
                {"slug": "ark:gemini-4-carbon", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "max")]},
                {"slug": "ark:gpt-6-1-sol", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "max")]},
                {"slug": "ark:kimi-k3-1-pro-highspeed", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
            ]
        }), encoding="utf-8")
        (codex_home / "config.toml").write_text(
            f'model = "ark:gemini-4-carbon"\nmodel_provider = "ark"\nmodel_reasoning_effort = "max"\n'
            f'model_catalog_json = "{cat}"\n\n[model_providers.ark]\nbase_url = "http://127.0.0.1:17413/client-retry"\n',
            encoding="utf-8",
        )
        fake_yaml = SimpleNamespace(safe_load=lambda t: json.loads(t) if t.strip() else {})
        fake_cfg = SimpleNamespace(atomic_config_write=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"))
        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", checked_publication=True,
            model_sync_enabled=False,
            review_reasoning_effort="medium",
            review_fallback_models=("ark:gpt-6-1-sol", "ark:kimi-k3-1-pro-highspeed"),
        )
        store = Store(settings.state)
        self.addCleanup(store.close)
        bridge = SimpleNamespace(settings=settings, store=store, retry_queue=None, api=None)
        with patch.dict(sys.modules, {"hermes_yaml": fake_yaml, "hermes_cli": SimpleNamespace(config=fake_cfg), "hermes_cli.config": fake_cfg}):
            # Decoupled: sync_with_codex does not follow foreground Gemini/max config.
            self.assertIsNone(sync_with_codex(bridge, codex_home=codex_home))
            doc = json.loads((self.home / "config.yaml").read_text())
            self.assertEqual(doc["model"]["default"], "ark:gpt-6-2-sol")
            self.assertEqual(doc["agent"]["reasoning_effort"], "medium")

            # Even if manually switched with requested_effort='max', review_reasoning_effort caps to 'medium'.
            res = switch_pilot_model(settings, store, "ark:gpt-6-2-sol", requested_effort="max",
                                     codex_home=codex_home, apply=True, verify_probe=False, pin=True)
            self.assertEqual(res["target_effort"], "medium")

            # On 403 outage, falls back to same-protocol list (gpt-6-1-sol), skipping Gemini.
            with patch("hermes_codex_learning.model_sync.probe_review_model", return_value={"ok": True, "http_status": 200}):
                failover = record_review_model_failure(
                    bridge, "HTTP 403: The requested model is currently unavailable", codex_home=codex_home
                )
            self.assertTrue(failover["applied"])
            self.assertEqual(failover["target_model"], "ark:gpt-6-1-sol")
            self.assertEqual(failover["target_effort"], "medium")
            self.assertEqual(failover["sync_mode"], "pinned")

    def test_hook_clean_trim_and_no_duplicate_injection_when_projected(self):
        text, _ = model_context(
            "Project codex-hermes-plugin: the managed macOS notifier is HermesLearningNotifier.",
            str(self.project), [], limit=160,
        )
        self.assertLessEqual(len(text.encode("utf-8")), 160)
        self.assertNotIn("the mana\n", text)

        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", session_context_limit=1024,
        )
        settings.state.mkdir(parents=True, exist_ok=True)
        context = {
            "version": "v1",
            "memories": {"MEMORY.md": "Project fact: always run unit tests before deployment.", "USER.md": "Prefer Chinese summary."},
            "memory_scopes": {"MEMORY.md": "project", "USER.md": "user"},
            "skills": [{"name": "learned-demo", "description": "demo skill", "version": "sv1"}],
            "native_projected_skills": ["learned-demo"],
            "bridge_provenance": {
                "memory": {digest(["MEMORY.md", "Project fact: always run unit tests before deployment."]): {"project": str(self.project)}},
                "skills": {"learned-demo": {"projects": [str(self.project)]}},
            },
        }
        atomic_json(settings.state / "context.json", context)
        first = hook({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(self.project)}, settings)
        self.assertIn("Project fact: always run unit tests before deployment.", first["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Prefer Chinese summary.", first["hookSpecificOutput"]["additionalContext"])
        # Subsequent UserPromptSubmit with no new unprojected skill and unchanged memory must not duplicate injection.
        second = hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                       "cwd": str(self.project), "prompt": "demo skill check"}, settings)
        self.assertEqual(second, {})

    def test_context_view_includes_user_md_and_retention_prunes_settled_raw_events(self):
        ctx = {
            "memories": {"MEMORY.md": "Local invariant", "USER.md": "Always keep diffs concise"},
            "memory_scopes": {"MEMORY.md": "project", "USER.md": "user"},
            "skills": [],
            "bridge_provenance": {"memory": {digest(["MEMORY.md", "Local invariant"]): {"project": str(self.project)}}, "skills": {}},
        }
        view = context_view(ctx, str(self.project), "")
        self.assertEqual(view["memories"]["USER.md"], "Always keep diffs concise")
        self.assertEqual(view["memories"]["MEMORY.md"], "Local invariant")

        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", raw_event_retention_days=7,
        )
        store = Store(settings.state)
        self.addCleanup(store.close)
        old_ts = time.time() - 10 * 86400
        turn_payload = {
            "source": {"host": "h", "project": str(self.project), "chat": "c-old", "turn": "t-old"},
            "user": "u", "assistant": "a", "tools": {}, "status": "completed",
        }
        with store.db:
            store.db.execute(
                "INSERT INTO turns (id, payload, revision, observed_revision, stopped, activity) VALUES (?,?,?,?,?,?)",
                ("tid-1", dumps(turn_payload), 2, 2, 1, old_ts),
            )
            store.db.execute(
                "INSERT INTO events (id, source_key, payload, created, delivered, error) VALUES (?,?,?,?,?,?)",
                ("ev-1", "sk-1", dumps({"session_id": "c-old", "turn_id": "t-old", "tool_output": "x" * 2000}), old_ts, old_ts, None),
            )
        store.set("transcript-cursor:" + digest(["c-old", "t-old"]), {"offset": 100})
        fake_api = SimpleNamespace(
            prune=lambda **kw: {"eligible_scopes": 0},
            prune_artifacts=lambda *a, **kw: {"removed": []},
        )
        out = run_retention(settings, store, fake_api, dry_run=False)
        self.assertEqual(out["pruned_raw_events"], 1)
        self.assertEqual(out["pruned_transcript_cursors"], 1)
        self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertIsNone(store.get("transcript-cursor:" + digest(["c-old", "t-old"])))

    def test_cross_project_shared_skill_edits_allow_published_refinements_only(self):
        project_b = self.root / "project-b"
        project_b.mkdir()
        settings = Settings(
            self.home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", checked_publication=True,
            allow_cross_project_shared_skill_edits=True,
        )
        boundary_file = settings.state / "enterprise-boundary.json"
        settings.state.mkdir(parents=True, exist_ok=True)
        atomic_json(boundary_file, make_boundary(settings))
        settings = replace(settings, data_policy={
            "enabled": True, "mode": "trusted_enterprise", "enterprise_boundary": str(boundary_file),
        })
        active_id = boundary(settings.data_policy)["id"]
        store = Store(settings.state)
        self.addCleanup(store.close)
        store.set("knowledge-index-v1", {"memory": {}, "skills": {
            "learned-shared": {"projects": [str(self.project)]},
            "learned-private": {"projects": [str(self.project)]},
        }})
        store.set("publications-v1", {
            "learned-shared": {"check": {"allowed": True, "audience": "enterprise", "boundary_id": active_id}},
        })
        bridge = SimpleNamespace(
            settings=settings,
            store=store,
            api=SimpleNamespace(read=lambda kind="context", **kw: {"skills": [
                {"name": "learned-shared"}, {"name": "learned-private"},
            ]}),
        )
        can_edit, _, _ = _skill_filters(bridge, str(project_b))
        # Another project can refine an enterprise-published shared skill...
        self.assertTrue(can_edit("patch", "learned-shared", None))
        self.assertTrue(can_edit("write_file", "learned-shared", None))
        # ...but cannot delete/archive a cross-project shared skill or edit an unpublished private skill.
        self.assertFalse(can_edit("delete", "learned-shared", None))
        self.assertFalse(can_edit("archive", "learned-shared", None))
        self.assertFalse(can_edit("patch", "learned-private", None))
        # When disabled, cross-project edits remain blocked.
        strict_bridge = SimpleNamespace(
            settings=replace(settings, allow_cross_project_shared_skill_edits=False),
            store=store, api=bridge.api,
        )
        strict_edit, _, _ = _skill_filters(strict_bridge, str(project_b))
        self.assertFalse(strict_edit("patch", "learned-shared", None))


if __name__ == "__main__":
    unittest.main()
