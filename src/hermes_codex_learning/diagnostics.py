"""Read-only diagnosis. Configuration, reachability and model evidence stay distinct."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .authentication import native_auth_status
from .config import Settings
from .ipc import request


def data_admission(settings: Settings) -> dict:
    from .enterprise import MODE, boundary

    policy = settings.data_policy
    result = {
        "workflow_projection": policy.get("workflow_projection") is True,
        "assessment_configured": bool(policy.get("assessment_command") or policy.get("source_manifest")),
        "unassessed_body": "withheld" if policy else "allowed_by_legacy_configuration",
        "semantic_evidence_checks": bool(policy),
    }
    if policy.get("mode") == MODE:
        approved = boundary(policy)
        result.update(mode=MODE, boundary_valid=bool(approved), assessment_configured=True,
                      unassessed_body="ordinary_enterprise_authorized" if approved else "withheld")
    return result


def learning_policy(settings: Settings) -> dict:
    from .quality import sandbox_backend

    backend = sandbox_backend()
    return {
        "proactive": settings.proactive,
        "checked_publication": settings.checked_publication,
        "quality_checks": settings.quality_checks,
        "retention_enabled": settings.retention_enabled,
        "review_timeout_seconds": settings.review_timeout_seconds,
        "executable_validation": {
            "backend": backend,
            "state": ("disabled" if not settings.quality_checks else
                      "available_for_verification" if backend else "backend_unavailable"),
        },
    }


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
    result["data_admission"] = data_admission(settings)
    from .identity import build_identity
    result["build"] = {"client":build_identity(),"running":None,"matches":None}
    result["configuration"]["selected_file"] = str(Settings.configuration_path(path))
    result["learning_policy"] = learning_policy(settings)
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
        status = request(settings, "learning_status", {"limit": 1}, timeout=2)
        result["service"] = {"reachable": True, "state": status.get("state"), "error": status.get("error")}
        result["build"]["running"] = status.get("build")
        if status.get("build"):
            result["build"]["matches"] = status["build"]["digest"] == result["build"]["client"]["digest"]
        result["capture_coverage_gaps"] = status.get("capture_coverage_gaps", [])
        result["status"] = "degraded" if status.get("error") else "reachable"
        if result["build"]["matches"] is False or status.get("state") == "update-paused":
            result["status"] = "degraded"
        result["coverage"] = status.get("coverage", {})
        progress = status.get("progress")
        result["learning_progress"] = ({"state":"backlogged" if progress.get("backlog",{}).get("pending_events") else "positions_available",
            "backlog":progress.get("backlog"),"worker_position":progress.get("worker_position"),
            "review_queue":progress.get("review_queue"),"semantics":progress.get("semantics")} if progress else
            {"state":"not_exposed_by_running_build"})
        result["retention"] = status.get("retention")
        result["running_policy"] = status.get("effective_policy")
        result["memory_capacity"] = status.get("memory_capacity")
        result["sharing"] = status.get("sharing")
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
