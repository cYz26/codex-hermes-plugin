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


def fixture(source: dict, turn: str, *, revision: bool) -> dict:
    procedure = (
        "Synthetic CSV fixture only: project rows use semicolon CSV. Header names are stripped and casefolded "
        "before duplicate checking. Reject empty or duplicate normalized headers before parsing rows. "
        "The reusable procedure is: read the header, normalize, reject empty/duplicates, then parse records; "
        "verify with case-only duplicates, whitespace-only names and a valid two-column file. "
        "For this acceptance fixture, retain one compact fixture-scoped memory and create the reusable skill "
        "named csv-header-validation-fixture, containing trigger, steps, pitfalls and verification."
    )
    if revision:
        procedure = (
            "Synthetic correction to csv-header-validation-fixture: normalize Unicode to NFKC BEFORE stripping "
            "and casefolding header names. Full-width Ａ and ASCII A must be treated as duplicates. "
            "The prior fixture procedure missed compatibility characters. Update the existing skill and its "
            "verification cases in place; do not create a second skill. Retain its semicolon delimiter and "
            "empty-header rules. Keep the knowledge explicitly scoped to this synthetic fixture."
        )
    return {"source": {**source, "turn": turn}, "status": "completed", "tools": [],
            "coverage": ["synthetic_fixture_not_codex_app_hooks"],
            "user": "This is an explicit synthetic Learning API acceptance fixture, not a captured App conversation. " + procedure,
            "assistant": "Fixture result: the procedure was exercised against synthetic valid and invalid header examples. "
                         "Only fixture-scoped knowledge is eligible for this probe. No deployment settings or real user preferences are being asserted."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", help="Stable probe identity; do not replay an unknown attempt with a new ID")
    args = parser.parse_args()
    if Path(args.output).exists():
        parser.error("Output receipt already exists; inspect it before starting an explicitly new probe")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.authentication import native_auth_status
    from hermes_codex_learning.store import atomic_json, redact

    settings = Settings.load(args.settings)
    home = settings.home
    os.environ.update(settings.native_environment())
    sys.path.insert(0, str(settings.source))
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    cfg = load_config_readonly()
    context = PluginContext(PluginManifest(name="codex-learning-bridge", source="user"),
                            PluginManager(scope_key=str(home)))
    api = context.learning(version=1)
    identity = args.run_id or "real-provider-probe-" + uuid.uuid4().hex
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "real_app": False,
        "source": "explicit_synthetic_acceptance_fixture",
        "trigger": "explicit_idle_flush_for_contract_verification",
        "model": {key: cfg["model"].get(key) for key in ("provider", "default", "api_mode")},
        "effort": cfg["agent"].get("reasoning_effort"),
        "authentication": native_auth_status(settings),
        "scope": identity,
        "reviews": [],
        "run_status": "started",
    }
    successful = False
    try:
        source = {"host": "local-acceptance-fixture", "project": str(settings.project),
                  "chat": identity, "executor": "integration acceptance fixture"}
        handle = api.open(source)["handle"]
        result["handle"] = handle
        before = api.read(kind="context")
        versions = []
        for phase, revision in (("initial", False), ("revision", True)):
            phase_id = identity + ":" + phase
            receipt = api.observe(handle, fixture(source, phase_id, revision=revision), event_id=phase_id + ":observation")
            intent = api.maintenance(handle=handle, idle_flush=True)["intent"]
            if not intent:
                raise RuntimeError("No verification review intent was admitted")
            result["active_attempt"] = phase_id + ":attempt"
            atomic_json(Path(args.output), redact(result))
            review = api.run_review(handle, intent, attempt_id=result["active_attempt"])
            result["reviews"].append({"phase": phase, "receipt": receipt, "review": review})
            atomic_json(Path(args.output), redact(result))
            if review.get("run_status") != "completed":
                raise RuntimeError("Native probe review did not complete: " + review.get("run_status", "unknown"))
            skill = api.read(kind="skill", name="csv-header-validation-fixture")
            versions.append(skill["content_hash"])
        after = api.read(kind="context")
        result["verification"] = {"skill_hashes": versions, "skill_revised": versions[0] != versions[1],
                                  "memory_changed": before["memories"] != after["memories"]}
        successful = all(result["verification"][key] for key in ("skill_revised", "memory_changed"))
        result["run_status"] = "completed" if successful else "verification_failed"
        result["usage"] = api.read(kind="status")["usage"]
        result["close_scope"] = api.close(handle=handle, event_id=identity + ":end")
    except Exception as exc:
        result["run_status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            result["owner_close"] = api.close()
        except Exception as exc:
            result["close_error"] = type(exc).__name__
            result["run_status"] = "failed"
            successful = False
        atomic_json(Path(args.output), redact(result))
    summary = {key: result.get(key) for key in ("timestamp", "run_status", "real_app", "model", "effort", "verification")}
    summary["applied"] = sum(len(item["review"].get("applied", [])) for item in result["reviews"])
    atomic_json(settings.state / "model-probe.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
