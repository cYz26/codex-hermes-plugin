"""Serve immutable skill artifacts and report local environment requirements."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from .policy import published_resource
from .quality import proof_valid
from .read_receipts import issue
from .store import ReadStore, digest


def declared_requirements(body):
    try:
        from agent.skill_utils import PLATFORM_MAP, parse_frontmatter
        from tools.skills_tool_setup import _get_required_environment_variables
    except ImportError:
        return None

    frontmatter, _ = parse_frontmatter(body or "")
    platforms = frontmatter.get("platforms") or []
    if not isinstance(platforms, list):
        platforms = [platforms]
    aliases = [str(platform).lower().strip() for platform in platforms]
    commands = frontmatter.get("required_commands") or []
    if isinstance(commands, str):
        commands = [commands]
    return {"platforms": [PLATFORM_MAP.get(alias, alias) for alias in aliases],
            "environment": [item["name"] for item in _get_required_environment_variables(frontmatter)
                            if not item.get("optional")],
            "commands": [name for name in commands if isinstance(name, str) and name] if isinstance(commands, list) else [],
            "unchecked": [label for label, key in (("credential_files", "required_credential_files"),
                           ("hermes_packages", "deps")) if frontmatter.get(key)]}


def check_requirements(requirements, *, scope):
    """Check the current process environment without opening credentials or running setup."""
    result = {"scope": scope, "platform": sys.platform, "checked_at": time.time()}
    if requirements is None:
        return {**result, "state": "unchecked"}
    platforms = requirements["platforms"]
    if platforms and not sys.platform.startswith(tuple(platforms)):
        return {**result, "state": "unsupported_platform"}
    missing_environment = [name for name in requirements["environment"] if not os.environ.get(name)]
    missing_commands = [name for name in requirements["commands"] if "\x00" in name or not shutil.which(name)]
    unchecked = requirements["unchecked"]
    if missing_environment or missing_commands:
        state = "requirements_missing"
    else:
        state = "partial" if unchecked else "declared_checks_passed"
    return {**result, "state": state, "missing_environment": missing_environment,
            "missing_commands": missing_commands, "unchecked": unchecked}


def load(settings, name, project, file_path=None, *, artifact_version=None, requirements_only=False):
    from .enterprise import boundary
    approved=boundary(settings.data_policy)
    active_boundary=approved["id"] if approved else None
    store=ReadStore(settings.state)
    try:
        name=store.get("publication-aliases",{}).get(name,name)
        origins=store.get("knowledge-index-v1",{}).get("skills",{}).get(name,{}).get("projects",[])
        publication=store.get("publications-v1",{}).get(name)
        required=store.get("semantic-skill-attestations",{}).get(name,{}).get("boundary_id")
        if publication and publication.get("check",{}).get("audience")=="enterprise":
            required=publication["check"].get("boundary_id")
        shared=settings.checked_publication and origins!=[project]
        if shared:
            if not publication:
                raise PermissionError("Skill has not passed shared publication checks")
            record=publication
        else:
            record=store.get("artifact-read-cache",{}).get(name)
            if not record:
                raise RuntimeError("Skill snapshot is not ready; retry after the next background publication")
        value=published_resource(record,file_path)
        required=record.get("check",{}).get("boundary_id") or required
        if required and required!=active_boundary:
            raise PermissionError("Enterprise knowledge is unavailable outside its approved deployment")
        if store.db.execute("SELECT 1 FROM withheld_versions WHERE name=? AND version=?",(name,value["version"])).fetchone():
            if publication and publication["artifact"]["version"]!=value["version"]:
                record = publication
                value = published_resource(record, file_path)
                shared = True
            else:
                raise PermissionError("This skill version has a failed host validation")
        if artifact_version is not None and artifact_version != value["version"]:
            raise ValueError("The recalled skill version is no longer current; refresh context before loading")
        body = value["content"] if value["file_path"] == "SKILL.md" else published_resource(record)["content"]
        requirements = declared_requirements(body)
        if requirements_only:
            return {"name": name, "version": value["version"], "requirements": requirements}
        proof=store.get("quality:"+digest([name,value["version"]]),{})
        if settings.quality_checks and Path(value["file_path"]).suffix in {".py",".sh"} and not proof_valid(value,proof):
            raise PermissionError("Executable resource has not passed independent validation")
        value.update(scope="global" if shared else "worktree",request_project=project,
                     source_projects=[] if shared else origins,
                     validation=proof or {"state":"pending","business_effectiveness":"not_inferred"})
        if required:
            value.update(audience="enterprise",boundary_id=required)
        if shared:
            value["source_ids"]=[digest(item) for item in record.get("sources",[])]
        value["host_readiness"] = check_requirements(requirements, scope="worker_host")
        if "readiness_status" in value:
            value["native_readiness_scope"] = "cached_learning_environment"
            value["native_readiness_checked_at"] = record.get("cached_at")
        value["delivery_token"]=issue(settings.state,value,project)
        return value
    finally:
        store.close()
