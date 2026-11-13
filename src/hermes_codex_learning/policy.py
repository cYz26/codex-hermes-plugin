"""Explicit data admission and versioned shared-skill publication.

Built-in checks are not a general classifier. Unknown content needs a configured
assessment command; rejection records never retain the rejected body.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .store import digest, dumps, redact


def admit(payload: dict, policy: dict, project: str, *, purpose: str = "observe") -> dict:
    if policy.get("mode") == "trusted_enterprise":
        from .enterprise import assess
        return assess(payload,policy,purpose)
    if not policy:
        return {"allowed": True, "basis": "legacy_configuration"}
    if redact(payload) != payload:
        return {"allowed": False, "reason": "credential_material"}
    roots = policy.get("synthetic_roots", [])
    if not isinstance(roots,list) or any(not isinstance(root,str) or not Path(root).is_absolute() or Path(root).resolve()==Path("/") for root in roots):
        return {"allowed":False,"reason":"invalid_synthetic_roots"}
    if any(Path(project).resolve().is_relative_to(Path(root).resolve()) for root in roots):
        return {"allowed": True, "basis": "synthetic_acceptance"}
    command = policy.get("assessment_command")
    if not command and policy.get("source_manifest") and purpose != "share":
        from .assessment import assess
        try:
            assessment = assess(payload, Path(policy["source_manifest"]))
            if assessment.get("allowed"):
                return {**assessment, "basis":"assessed_source_fragments", "body_retained":True}
            return assessment
        except (OSError, ValueError, KeyError, TypeError):
            return {"allowed":False, "reason":"source_manifest_unavailable"}
    if not command:
        return {"allowed": False, "reason": "data_classification_unassessed"}
    if (not isinstance(command, list) or not command or not Path(command[0]).is_absolute()
            or not all(isinstance(item, str) for item in command)):
        return {"allowed": False, "reason": "invalid_assessment_command"}
    try:
        result = subprocess.run(command, input=dumps({"project": project, "purpose": purpose, "event": payload}),
                                capture_output=True, text=True, timeout=3, check=True)
        assessment = json.loads(result.stdout)
        if (not isinstance(assessment,dict) or type(assessment.get("schema_version",1)) is not int
                or assessment.get("schema_version",1) not in {1,2}):
            return {"allowed":False,"reason":"unsupported_assessment_schema"}
        if purpose == "share" and (assessment.get("classification") != "public" or assessment.get("sharing_allowed") is not True):
            return {"allowed": False, "reason": "shared_candidate_not_assessed_public"}
        if (assessment.get("allowed") is True and assessment.get("assessment_id")
                and assessment.get("classification") in {"public", "synthetic", "approved"}):
            result = {"allowed": True, "basis": "assessment", "assessment_id": str(assessment["assessment_id"])[:128],
                      "classification":assessment["classification"]}
            if assessment.get("schema_version") == 2:
                from .assessment import validate_replacement
                result.update(schema_version=2, payload=validate_replacement(payload, assessment),
                              policy_version=assessment["policy_version"], source_refs=assessment.get("source_refs", []),
                              body_retained=True, input_hash=assessment["input_hash"], output_hash=assessment["output_hash"])
            return result
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"allowed": False, "reason": "assessment_not_approved"}


def sharing_check(value: dict, origins: list[str], private_markers: list[str], *, executable_candidate=False,
                  enterprise=False, native_candidate=False) -> dict:
    content = value.get("content") or ""
    front = content.split("---", 2)
    declared = len(front) >= 3 and re.search(r"(?m)^learning_scope:\s*global_candidate\s*$", front[1])
    inherited = native_candidate and len(front) >= 3 and not re.search(r'(?m)^\s*learning_scope\s*:', front[1])
    if not declared and not inherited:
        return {"allowed": False, "reason": "project_experience"}
    if not origins:
        return {"allowed": False, "reason": "unknown_source"}
    files = value.get("files", {})
    root = Path(value["resource_dir"]).resolve()
    if not files or len(files) > 100:
        return {"allowed": False, "reason": "resource_manifest_missing_or_oversized"}
    text = []
    for relative, checksum in files.items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            return {"allowed": False, "reason": "resource_escape_or_missing"}
        if path.stat().st_size > 2 * 1024 * 1024:
            return {"allowed": False, "reason": "resource_too_large"}
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != checksum:
            return {"allowed": False, "reason": "resource_changed"}
        # Executable resources require a separate trusted test receipt. The first
        # release does not pretend syntax checks prove that arbitrary code works.
        if path.suffix.lower() not in ({".md", ".txt", ".json", ".csv", ".yaml", ".yml"}
                                       | ({".py", ".sh"} if executable_candidate else set())):
            return {"allowed": False, "reason": "executable_resource_needs_validation"}
        try:
            text.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return {"allowed": False, "reason": "binary_resource_unassessed"}
    combined = "\n".join(text)
    if enterprise:
        from .enterprise import scrub
        if scrub(combined)!=combined:
            return {"allowed":False,"reason":"credential_material"}
    if redact(combined) != combined:
        return {"allowed": False, "reason": "credential_material"}
    # Internal method/API names are useful inside the authorized enterprise scope;
    # absolute project roots still do not become portable shared instructions.
    markers = [*origins, *([] if enterprise else private_markers)]
    if any(marker and len(marker) >= 5 and marker in combined for marker in markers):
        return {"allowed": False, "reason": "project_private_material"}
    forbidden = r"/(?:Users|home|data00|etc)/" if enterprise else r"https?://[^\s/]*(?:bytedance|larkoffice|feishu|\.internal|\.corp)|/(?:Users|home|data00|etc)/"
    if re.search(forbidden, combined, re.I):
        return {"allowed": False, "reason": "internal_address_or_absolute_path"}
    return {"allowed": True, "basis": "native_curator_candidate_and_resource_checks" if inherited else "explicit_generic_candidate_and_resource_checks",
            "version": value["version"], "manifest_hash": digest(files)}


def publication_catalog(publications, aliases):
    return {name: {"catalog_entry": {**item["catalog_entry"], "artifact_version": item["artifact"]["version"],
                                     "aliases": [key for key, target in aliases.items() if target == name]},
                   "version": item["artifact"]["version"], "audience": item.get("check", {}).get("audience", "public"),
                   "boundary_id": item.get("check", {}).get("boundary_id")}
            for name, item in publications.items()}


def published_resource(record: dict, file_path: str | None = None) -> dict:
    value = record["artifact"]
    relative = file_path or "SKILL.md"
    if relative not in value["files"]:
        raise ValueError("Unknown shared resource")
    root = Path(value["resource_dir"]).resolve()
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Shared resource escapes its publication")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != value["files"][relative]:
        raise ValueError("Published resource changed")
    return {**value, "content": raw.decode("utf-8"), "file_path": relative,
            "content_hash": value["files"][relative], "scope": "global"}
