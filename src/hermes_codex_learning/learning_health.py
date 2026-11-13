"""Read-only operational facts from the learning worker and its native profile."""
from __future__ import annotations

import json
import re
from collections import Counter

from .diagnostics import data_admission, learning_policy
from .knowledge import ENTRY_DELIMITER, memory_entries
from .store import digest


def native_policy() -> dict:
    """Call Hermes' configuration readers inside its learning worker."""
    from agent import curator
    from agent.background_review import load_background_review_settings
    from hermes_cli.config import load_config_readonly
    from tools.memory_tool import get_builtin_memory_config, get_builtin_memory_store_flags

    configuration = load_config_readonly()
    memory = get_builtin_memory_config(configuration)
    memory_enabled, user_enabled = get_builtin_memory_store_flags(configuration)
    review_enabled, review = load_background_review_settings()
    model = configuration.get("model")
    model = model if isinstance(model, dict) else {"default": model}
    provider, review_model = review.get("provider"), review.get("model")
    routed = provider not in (None, "", "auto") and bool(review_model)
    skills = configuration.get("skills")
    skills = skills if isinstance(skills, dict) else {}
    return {
        "memory": {
            "MEMORY.md": {"enabled": memory_enabled, "limit_chars": int(memory.get("memory_char_limit", 2200))},
            "USER.md": {"enabled": user_enabled, "limit_chars": int(memory.get("user_char_limit", 1375))},
        },
        "review": {
            "enabled": review_enabled,
            "configured_provider": provider if routed else model.get("provider"),
            "configured_model": review_model if routed else model.get("default"),
            "max_input_tokens": review.get("max_input_tokens"),
            "memory_turn_interval": int(memory.get("nudge_interval", 10)),
            "skill_tool_interval": int(skills.get("creation_nudge_interval", 10)),
        },
        "curator": {
            "enabled": curator.is_enabled(), "consolidate": curator.get_consolidate(),
            "interval_hours": curator.get_interval_hours(), "min_idle_hours": curator.get_min_idle_hours(),
        },
    }


def memory_capacity(settings, store, context, project, native):
    from .approvals import ID_PATTERN, PendingReviews

    origins = context.get("bridge_provenance", {}).get("memory", {})
    seen = set()
    file_entries, file_chars = Counter(), Counter()
    project_entries, project_chars = Counter(), Counter()
    for identity, filename, entry in memory_entries(context):
        entry = entry.strip()
        if (filename, entry) in seen:
            continue
        seen.add((filename, entry))
        owner = origins.get(identity, {}).get("project")
        if not owner and not origins and settings.capture_scope == "project":
            owner = str(settings.project)
        file_entries[filename] += 1
        file_chars[filename] += len(entry)
        project_entries[owner, filename] += 1
        project_chars[owner, filename] += len(entry)
    projects = sorted({owner for owner, _ in project_entries if owner is not None})
    files = {}
    for filename in ("MEMORY.md", "USER.md"):
        configuration = native.get("memory", {}).get(filename, {})
        entries = file_entries[filename]
        used = file_chars[filename] + max(0, entries - 1) * len(ENTRY_DELIMITER)
        limit = configuration.get("limit_chars")
        files[filename] = {
            **configuration, "used_chars": used, "entries": entries,
            "remaining_chars": max(0, limit - used) if type(limit) is int else None,
            "over_limit_chars": max(0, used - limit) if type(limit) is int else None,
        }

    pending_dir = settings.home / "pending" / "memory"
    pending = {path.stem for path in pending_dir.glob("*.json")
               if ID_PATTERN.fullmatch(path.stem) and not path.is_symlink() and path.is_file()}
    scoped_pending = None
    if project:
        scoped_pending = len(pending.intersection(PendingReviews(settings, store).identifiers(project))) if pending else 0
    result = {
        "state": "available", "files": files, "project_count": len(projects),
        "unassigned_entries": sum(project_entries[None, filename] for filename in files),
        "pending_memory_proposals": len(pending),
        "pending_project_proposals": scoped_pending,
        "latest_capacity_refusal": store.get("memory-capacity-refusal:" + project) if project else store.get("memory-capacity-refusal"),
    }
    if project:
        result["project"] = project
        result["project_files"] = {filename: {
            "entries": project_entries[project, filename], "entry_chars": project_chars[project, filename],
        } for filename in files}
    else:
        result["projects"] = [{"project": owner, "files": {
            filename: {"entries": project_entries[owner, filename], "entry_chars": project_chars[owner, filename]}
            for filename in files}} for owner in projects]
    return result


def memory_preview(settings, context, project, native, proposals=None):
    """Preview shorter replacements against the project snapshot and native accounting."""
    from .enterprise import scope_view

    context = scope_view(context, settings.data_policy)
    active_boundary = context.get("active_enterprise_boundary")
    origins = context.get("bridge_provenance", {}).get("memory", {})
    boundaries = context.get("enterprise_memory_boundaries", {})
    entries = []
    seen_ids = set()
    for identity, filename, text in memory_entries(context):
        owner = origins.get(identity, {}).get("project")
        if owner is None and not origins and settings.capture_scope == "project":
            owner = str(settings.project)
        if identity in seen_ids or owner != project or boundaries.get(identity) not in {None, active_boundary}:
            continue
        seen_ids.add(identity)
        entries.append({"id": identity, "target": filename, "source_project": owner, "text": text,
                        "chars": len(text.strip())})
    revision = digest(entries)
    savings = Counter()
    if proposals is not None:
        if not isinstance(proposals, dict) or proposals.get("revision") != revision:
            raise ValueError("Memory preview changed; refresh the project snapshot")
        replacements = proposals.get("replacements")
        if not isinstance(replacements, dict) or not replacements.keys() <= {entry["id"] for entry in entries}:
            raise ValueError("Every replacement must refer to an entry in this project snapshot")
        seen = {(target, text.strip()) for _, target, text in memory_entries(context)}
        for entry in entries:
            if entry["id"] not in replacements:
                continue
            text = replacements[entry["id"]]
            if not isinstance(text, str) or not text.strip() or ENTRY_DELIMITER in text.strip():
                raise ValueError("A replacement must contain a single nonempty memory entry")
            text = text.strip()
            if len(text) >= entry["chars"] or (entry["target"], text) in seen:
                raise ValueError("A replacement must be shorter and distinct within the memory file")
            seen.add((entry["target"], text))
            saved = entry["chars"] - len(text)
            entry["proposal"] = {"proposed_text": text, "native_chars_saved": saved}
            savings[entry["target"]] += saved
    return {"state": "available", "project": project, "revision": revision, "entries": entries,
            "estimated_native_chars_saved": dict(savings), "native_limits": native.get("memory", {}),
            "review_source": "published_memory_snapshot"}


def publication_preview(settings, store, context, project):
    """Inventory scope and check receipts without publishing or calling a model."""
    from .enterprise import scope_view
    from .knowledge import context_view
    from .policy import publication_catalog

    view = scope_view(context, settings.data_policy)
    # Scope filters apply before this inventory. It never returns resource bodies.
    visible = {item["name"]: item for item in context_view(view, project)["skills"]}
    publications = context.get("publications", {})
    if "publications" not in context:
        publications = publication_catalog(store.get("publications-v1", {}), store.get("publication-aliases", {}))
    checked_view = context_view({**view, "checked_publication": True, "publications": publications}, project)
    checked = {item["name"]: item for item in checked_view["skills"]}
    decisions = store.get("publication-decisions", {})
    origins = context.get("bridge_provenance", {}).get("skills", {})
    result = []
    for name in sorted(visible.keys() | checked.keys()):
        item = visible.get(name, {})
        published = publications.get(name, {})
        decision = decisions.get(name, {})
        version = item.get("artifact_version")
        sources = origins.get(name, {}).get("projects", [])
        if decision.get("version") and decision["version"] == version and decision.get("allowed") is False:
            state = "withheld"
        elif name in checked and published and published["version"] == version:
            state = "published"
        elif name in checked and sources == [project]:
            state = "project_visible"
        elif name in checked and published:
            state = "previous_publication_visible"
        elif not sources:
            state = "origin_unknown"
        else:
            state = "publication_check_needed"
        result.append({"name": name, "artifact_version": version, "source_projects": sources,
                       "artifact_version_if_checked": checked.get(name, {}).get("artifact_version"),
                       "visible_now": name in visible, "visible_if_checked": name in checked, "state": state,
                       "decision_reason": decision.get("reason") if state == "withheld" else None})
    return {"state": "available", "project": project, "entries": result, "visible_now": len(visible),
            "visible_if_checked": len(checked), "basis": "unfiltered_project_recall_with_existing_publications"}


_MEMORY_USAGE = re.compile(r"([\d,]+)/([\d,]+)")
_CAPACITY_MESSAGE = re.compile(r"^(?:Memory at [\d,]+/[\d,]+ chars;|.*would put memory at [\d,]+/[\d,]+ chars:)")


def capacity_refusal(outcome):
    message = outcome.get("error")
    return outcome.get("reason") == "memory_capacity_exceeded" or (
        isinstance(message, str) and bool(_CAPACITY_MESSAGE.match(message)))


def record_capacity_refusal(store, result, project, timestamp):
    for fact in result.get("refused", []):
        if fact.get("tool") != "memory":
            continue
        outcome = fact.get("result", {})
        if not capacity_refusal(outcome):
            continue
        refusal = {"at": timestamp, "target": fact.get("target"), "reason": "memory_capacity_exceeded"}
        usage = _MEMORY_USAGE.fullmatch(str(outcome.get("usage", "")))
        if usage:
            refusal.update(zip(("used_chars", "limit_chars"), (int(value.replace(",", "")) for value in usage.groups())))
        store.set("memory-capacity-refusal", refusal)
        if project:
            store.set("memory-capacity-refusal:" + project, refusal)


def snapshot(settings, store, context, project, native):
    policy = {
        **learning_policy(settings), "data_admission": data_admission(settings),
        "native_configuration": "read" if native else "unavailable", "native": native,
        "idle_review_seconds": settings.idle_seconds, "idle_review_interval_seconds": settings.idle_interval,
        "skill_counter_basis": "observed_terminal_tool_calls",
        "memory_counter_basis": "observed_user_turns",
    }
    capacity = (memory_capacity(settings, store, context, project, native or {})
                if context is not None and "memories" in context else {"state": "snapshot_unavailable"})
    if context is None or "skills" not in context:
        sharing = {"state": "snapshot_unavailable"}
    else:
        publications = context.get("publications", {})
        names = {item["name"] for item in context.get("skills", [])}
        sharing = {"catalog_entries": len(names), "published_entries": len(names.intersection(publications)),
                   "entries_without_shared_publication": len(names.difference(publications))}
        if project:
            from .enterprise import scope_view
            from .knowledge import context_view

            scoped = scope_view(context, settings.data_policy)
            current = context_view(scoped, project)
            checked = context_view({**scoped, "checked_publication": True}, project)
            sharing.update(
                project=project,
                catalog_visible_now=len(current["skills"]),
                catalog_visible_with_existing_publications=len(checked["skills"]),
            )
    return {"effective_policy": policy, "memory_capacity": capacity, "sharing": sharing}
