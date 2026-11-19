import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.enterprise import boundary, make_boundary
from hermes_codex_learning.model_sync import (
    record_review_model_failure,
    switch_pilot_model,
    sync_with_codex,
)
from hermes_codex_learning.review_retries import ReviewRetryQueue
from hermes_codex_learning.store import Store, atomic_json


class ModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "pilot"
        self.project = self.root / "project"
        self.codex_home = self.root / "codex"
        for d in (self.home, self.project, self.codex_home):
            d.mkdir(parents=True, exist_ok=True)

        self.catalog_path = self.codex_home / "ark-models.json"
        self.catalog_path.write_text(json.dumps({
            "models": [
                {"slug": "ark:gpt-6-1-sol", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
                {"slug": "ark:gemini-4-carbon", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "max")]},
                {"slug": "ark:gpt-6-astra", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
            ]
        }), encoding="utf-8")
        (self.codex_home / "config.toml").write_text(
            f'model = "ark:gpt-6-1-sol"\n'
            f'model_provider = "ark"\n'
            f'review_model = "ark:gpt-6-1-sol"\n'
            f'model_reasoning_effort = "ultra"\n'
            f'model_catalog_json = "{self.catalog_path}"\n\n'
            f'[model_providers.ark]\n'
            f'base_url = "http://127.0.0.1:17413/client-retry"\n',
            encoding="utf-8",
        )
        (self.home / "config.yaml").write_text(json.dumps({
            "_config_version": 1,
            "model": {"provider": "custom:ark", "default": "ark:gpt-6-1-sol", "api_mode": "codex_responses"},
            "agent": {"reasoning_effort": "ultra"},
            "providers": {"ark": {"base_url": "http://127.0.0.1:17413/client-retry", "api_mode": "codex_responses"}},
        }), encoding="utf-8")

        self.fake_yaml = SimpleNamespace(safe_load=lambda text: json.loads(text) if text.strip() else {})
        self.fake_cli_cfg = SimpleNamespace(
            atomic_config_write=lambda path, data: path.write_text(json.dumps(data), encoding="utf-8"),
        )
        self._saved_mods = {k: sys.modules.get(k) for k in ("hermes_yaml", "hermes_cli", "hermes_cli.config")}
        sys.modules["hermes_yaml"] = self.fake_yaml
        sys.modules["hermes_cli"] = SimpleNamespace(config=self.fake_cli_cfg)
        sys.modules["hermes_cli.config"] = self.fake_cli_cfg

        self.settings = Settings(self.home, self.project, self.root, Path("/usr/bin/python3"), capture_scope="global", checked_publication=True)
        boundary_file = self.settings.state / "enterprise-boundary.json"
        self.settings.state.mkdir(parents=True, exist_ok=True)
        atomic_json(boundary_file, make_boundary(self.settings))
        self.settings = replace(self.settings, data_policy={
            "enabled": True, "mode": "trusted_enterprise", "enterprise_boundary": str(boundary_file),
        })

    def tearDown(self):
        for k, mod in self._saved_mods.items():
            if mod is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = mod
        self.temp.cleanup()

    def test_switch_model_normalizes_alias_clamps_effort_refreshes_boundary_and_requeues_403(self):
        store = Store(self.settings.state)
        self.addCleanup(store.close)
        queue = ReviewRetryQueue(self.settings, store)
        queue.record(
            {"id": "intent-1", "handle": "h1", "reason": "native_threshold", "revision": 1},
            {"id": "att-1", "run_status": "failed", "error": "HTTP 403: The requested model is currently unavailable for your account. Please try another model."},
            {"project": str(self.project), "chat": "c1"},
        )
        self.assertEqual(queue.summary()["counts"].get("blocked"), 1)

        closed_scopes = []
        fake_runtime = SimpleNamespace(
            agents={"h1": SimpleNamespace(model="ark:gpt-6-1-sol"), "h2": SimpleNamespace(model="ark:gemini-4-carbon")},
            close_scope=lambda h: closed_scopes.append(h),
        )
        fake_api = SimpleNamespace(_service=SimpleNamespace(runtime=fake_runtime))

        with self.assertRaises(ValueError):
            switch_pilot_model(self.settings, store, "unlisted-external-model", codex_home=self.codex_home, apply=True, verify_probe=False)

        res = switch_pilot_model(
            self.settings, store, "gemini-4-carbon",
            codex_home=self.codex_home, apply=True, verify_probe=False,
            retry_queue=queue, api=fake_api,
        )
        self.assertTrue(res["applied"])
        self.assertEqual(res["target_model"], "ark:gemini-4-carbon")
        self.assertEqual(res["target_effort"], "max")
        self.assertTrue(res["enterprise_boundary_refreshed"])
        self.assertIsNotNone(boundary(self.settings.data_policy))
        self.assertEqual(closed_scopes, ["h1"])
        self.assertEqual(res["requeued_blocked_reviews"], 1)
        self.assertEqual(queue.summary()["counts"].get("waiting"), 1)
        self.assertTrue(Path(res["backup"]).is_file())

    def test_auto_sync_follows_codex_turn_context_and_403_fails_over_to_healthy_catalog_model(self):
        bridge = Bridge(self.settings, SimpleNamespace(version=1))
        self.addCleanup(bridge.store.close)
        bridge.store.set("codex-observed-model", {
            "model": "ark:gemini-4-carbon",
            "effort": "max",
            "at": time.time() + 10,
            "chat": "chat-1",
            "turn": "turn-1",
        })
        synced = sync_with_codex(bridge, codex_home=self.codex_home)
        self.assertIsNotNone(synced)
        self.assertEqual(synced["target_model"], "ark:gemini-4-carbon")
        written = json.loads((self.home / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["model"]["default"], "ark:gemini-4-carbon")

        with patch("hermes_codex_learning.model_sync.probe_review_model", return_value={"ok": True, "http_status": 200}):
            failover = record_review_model_failure(
                bridge,
                "HTTP 403: The requested model is currently unavailable for your account. Please try another model.",
                codex_home=self.codex_home,
            )
        self.assertTrue(failover["applied"])
        self.assertIn(failover["target_model"], {"ark:gpt-6-1-sol", "ark:gpt-6-astra"})
        self.assertIsNotNone(boundary(self.settings.data_policy))


if __name__ == "__main__":
    unittest.main()
