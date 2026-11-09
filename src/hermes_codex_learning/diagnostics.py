"""Read-only diagnosis. Configuration, reachability and model evidence stay distinct."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .authentication import native_auth_status
from .config import Settings
from .ipc import request


def inspect(path: str | Path | None = None) -> dict:
    result = {"status": "unconfigured", "configuration": {"valid": False},
              "service": {"reachable": False}, "snapshot": None, "model_probe": None}
    try:
        settings = Settings.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["configuration"]["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["configuration"] = {"valid": True, "home": str(settings.home), "project": str(settings.project),
                               "source_exists": settings.source.is_dir(), "interpreter_exists": settings.python.is_file()}
    result['data_admission'] = {'workflow_projection': settings.data_policy.get('workflow_projection') is True,
                                'assessment_configured': bool(settings.data_policy.get('assessment_command')),
                                'unassessed_body': 'withheld'}
    try:
        result["provider"] = native_auth_status(settings)
    except Exception as exc:
        # Subprocess stdout/stderr may contain provider diagnostics; do not echo them.
        result["provider"] = {"status": "unknown", "error_type": type(exc).__name__}
    snapshot = settings.state / "status.json"
    if snapshot.is_file():
        try:
            value = json.loads(snapshot.read_text())
            updated = value.get("updated_at")
            result["snapshot"] = {"updated_at": updated,
                                  "age_seconds": max(0, time.time() - updated) if isinstance(updated, (int, float)) else None,
                                  "stale": True}
        except (OSError, ValueError, TypeError, AttributeError):
            result["snapshot"] = {"stale": True, "error": "invalid_snapshot"}
    result["status"] = "unavailable"
    try:
        status = request(settings, "learning_status", timeout=2)
        result["service"] = {"reachable": True, "state": status.get("state"), "error": status.get("error")}
        result["capture_coverage_gaps"] = status.get("capture_coverage_gaps", [])
        result["status"] = "degraded" if status.get("error") else "reachable"
        # A responding service does not guarantee its last published snapshot is current.
        result["published_at"] = (status.get("snapshot") or {}).get("updated_at")
        if result["snapshot"] and result["snapshot"].get("age_seconds") is not None:
            result["snapshot"]["stale"] = result["snapshot"]["age_seconds"] > 30
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        result["service"]["error_type"] = type(exc).__name__
    probe = settings.state / "model-probe.json"
    if probe.is_file():
        try:
            value = json.loads(probe.read_text())
            result["model_probe"] = {key: value.get(key) for key in ("timestamp", "run_status", "real_app", "model", "effort", "applied")}
        except (OSError, ValueError, TypeError, AttributeError):
            result["model_probe"] = {"error": "invalid_probe_record"}
    return result


def doctor(args) -> int:
    result = inspect(args.settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "reachable" else 1
