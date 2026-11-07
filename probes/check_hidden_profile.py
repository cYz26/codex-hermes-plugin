"""Offline native contract for a hidden learning scope; temporary dummy credentials only.

Run with the dedicated Hermes interpreter and --source pointing to its patched checkout.
No real profile, provider request, App control or real credential store is used.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hermes-hidden-profile-") as directory:
        base = Path(directory).resolve()
        root, home = base / ".hermes", base / "learning-data"
        hidden, visible = root / "profiles/.codex-learning-pilot", root / "profiles/codex-learning-pilot"
        home.mkdir()
        hidden.parent.mkdir(parents=True)
        hidden.symlink_to(home, target_is_directory=True)
        visible.symlink_to(home, target_is_directory=True)
        os.environ["HERMES_HOME"] = str(hidden)
        os.environ["HERMES_RUNTIME_DIR"] = str(base / "tools")
        sys.path.insert(0, str(Path(args.source).resolve()))
        from hermes_constants import get_default_hermes_root
        from hermes_cli.auth import _save_codex_tokens, get_provider_auth_state, read_credential_pool
        from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from hermes_cli.plugins_loader import _plugin_home_scope
        from hermes_cli.profiles import list_profile_names, list_profiles, profiles_to_serve

        assert get_default_hermes_root() == root, "hidden spelling lost the existing auth root"
        cfg = {"_config_version": DEFAULT_CONFIG["_config_version"], "gateway": {"standalone": True},
               "plugins": {"entries": {"fixture": {"settings": {"learning": {"enabled": True}}}}}}
        atomic_config_write(home / "config.yaml", cfg)
        atomic_config_write(root / "config.yaml", {"_config_version": DEFAULT_CONFIG["_config_version"]})
        store = {"version": 1, "active_provider": "unrelated-provider", "providers": {
            "openai-codex": {"tokens": {"access_token": "fixture-old-at", "refresh_token": "fixture-old-rt"}}},
            "credential_pool": {"openai-codex": [{"id": "root-row", "source": "device_code",
                "auth_type": "oauth", "priority": 0, "access_token": "fixture-old-at", "refresh_token": "fixture-old-rt"}]}}
        (root / "auth.json").write_text(json.dumps(store), encoding="utf-8")
        (root / "memories").mkdir()
        (root / "memories/MEMORY.md").write_text("ROOT-ONLY", encoding="utf-8")
        (home / "memories").mkdir()
        (home / "memories/MEMORY.md").write_text("LEARNING-ONLY", encoding="utf-8")
        assert "codex-learning-pilot" in list_profile_names()
        visible.unlink()  # Remove only the registration link, exactly as in the deployment change.
        names = list_profile_names()
        detailed = [profile.name for profile in list_profiles(lazy_skill_count=True)]
        assert names == detailed == ["default"], (names, detailed)
        assert all(path.resolve() != home for _name, path in profiles_to_serve(multiplex=True))
        with _plugin_home_scope(home):
            assert get_provider_auth_state("openai-codex")["tokens"]["access_token"] == "fixture-old-at"
            assert read_credential_pool("openai-codex")[0]["access_token"] == "fixture-old-at"
            _save_codex_tokens({"access_token": "fixture-new-at", "refresh_token": "fixture-new-rt"}, write_through=True)
        stored = json.loads((root / "auth.json").read_text(encoding="utf-8"))
        assert stored["providers"]["openai-codex"]["tokens"]["refresh_token"] == "fixture-new-rt"
        assert stored["credential_pool"]["openai-codex"][0]["refresh_token"] == "fixture-new-rt"
        assert stored["active_provider"] == "unrelated-provider"
        assert not (home / "auth.json").exists()
        context = PluginContext(PluginManifest(name="fixture", source="user"), PluginManager(scope_key=str(home)))
        api = context.learning(version=1)
        try:
            api.open({"host": "fixture", "project": str(home), "chat": "hidden-fixture"})
            assert api.read(kind="context")["memories"]["MEMORY.md"] == "LEARNING-ONLY"
            assert (home / "learning/journal.sqlite").exists()
            assert not (root / "learning").exists()
        finally:
            api.close()
        assert (root / "memories/MEMORY.md").read_text(encoding="utf-8") == "ROOT-ONLY"
    print(json.dumps({"native_contract": "passed", "listed": False, "shared_auth_refresh": "passed",
                      "independent_learning_api": "passed", "real_credentials_used": False, "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
