"""Opt-in real-provider Learning API check; explicitly not captured App evidence.

Stop the pilot owner before running. Uses public plugin APIs and native authentication;
does not read, copy or print credentials. The native owner lock rejects concurrent use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.authentication import native_auth_status

    settings = Settings.load(args.settings)
    home = settings.home
    os.environ.update(settings.native_environment())
    sys.path.insert(0, str(settings.source))
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    cfg = load_config_readonly()
    assert cfg["agent"]["reasoning_effort"] == "max"
    assert cfg["model"]["provider"] == "openai-codex"
    assert cfg["model"]["default"] == "gpt-6.1-sol"
    context = PluginContext(PluginManifest(name="codex-learning-bridge", source="user"),
                            PluginManager(scope_key=str(home)))
    api = context.learning(version=1)
    identity = "real-provider-probe-" + uuid.uuid4().hex
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "real_app": False,
        "source": "explicit_synthetic_acceptance_fixture",
        "trigger": "explicit_idle_flush_for_contract_verification",
        "model": {key: cfg["model"].get(key) for key in ("provider", "default", "api_mode")},
        "effort": "max",
        "authentication": native_auth_status(settings),
        "scope": identity,
    }
    try:
        source = {"host": "local-acceptance-fixture", "project": str(settings.project),
                  "chat": identity, "executor": "integration acceptance fixture"}
        handle = api.open(source)["handle"]
        result["handle"] = handle
        result["receipt"] = api.observe(handle, {
            "source": {**source, "turn": identity + ":turn"},
            "status": "completed", "tools": [],
            "coverage": ["synthetic_fixture_not_codex_app_hooks"],
            "user": (
                "Acceptance fixture for the Codex–Hermes integration, not a captured App conversation. "
                "The real user selected a durable pilot setting: Hermes learns with reasoning effort max, "
                "model gpt-6.1-sol, provider openai-codex and native codex_responses. "
                "The dedicated pilot home is " + str(home) + ". "
                "Codex executes business work; Hermes performs unattended learning. "
                "Never interpret this fixture as proof of App hook delivery or successful skill application."
            ),
            "assistant": (
                "The pilot profile and approved plan were configured with max on official Hermes v0.22.7. "
                "Authentication is inherited natively from the user's existing Hermes App login; "
                "learning memory, skills and sessions remain in the separate pilot data directory. "
                "Real App hook trust, A-to-B recall and notification visibility still require acceptance."
            ),
        }, event_id=identity + ":observation")
        intent = api.maintenance(handle=handle, idle_flush=True)["intent"]
        if not intent:
            raise RuntimeError("No verification review intent was admitted")
        result["review"] = api.run_review(handle, intent, attempt_id=identity + ":attempt")
        result["usage"] = api.read(kind="status")["usage"]
        result["close_scope"] = api.close(handle=handle, event_id=identity + ":end")
    finally:
        result["owner_close"] = api.close()
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review = result["review"]
    print(json.dumps({"run_status": review["run_status"], "real_app": False,
                      "applied": len(review.get("applied", [])), "staged": len(review.get("staged", [])),
                      "usage": review.get("usage")}, ensure_ascii=False))
    return 0 if review["run_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
