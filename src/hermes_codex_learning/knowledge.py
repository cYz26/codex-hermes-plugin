"""Peripheral provenance and recall views; Hermes alone owns memory/skill writes.

Provenance is deliberately conservative. Unknown memory changes (including a
crash between a native write and its receipt) are withheld from automatic recall.
Shared skills remain discoverable by relevance; this is not a tenant ACL.
"""
from __future__ import annotations

import re

from .store import digest

ENTRY_DELIMITER = "\n§\n"  # Pinned Hermes v0.22.7 memory format.


def memory_entries(context: dict):
    for target, text in context.get("memories", {}).items():
        for entry in text.split(ENTRY_DELIMITER):
            if entry.strip():
                yield digest([target, entry]), target, entry


def changed_skills(result: dict) -> set[str]:
    names = set()
    for fact in result.get("applied", []):
        if fact.get("tool") != "skill_manage":
            continue
        operation = fact.get("operation") or fact
        if operation.get("name"):
            names.add(operation["name"])
        for child in fact.get("result", {}).get("results", []):
            if child.get("success") and child.get("name"):
                names.add(child["name"])
    return names


def reconcile(index: dict, context: dict, *, project: str | None = None,
              result: dict | None = None, bootstrap_project: str | None = None) -> dict:
    old_memory = index.get("memory", {})
    # Only the initial, explicit pilot migration may assign a legacy origin.
    observed_memory_write = any(fact.get("tool") == "memory" for fact in (result or {}).get("applied", []))
    origin = project if observed_memory_write else (bootstrap_project if not index else None)
    present = {key for key, _, _ in memory_entries(context)}
    removed_origins = {value.get("project") for key, value in old_memory.items() if key not in present}
    if result is not None and removed_origins - {project}:
        # A rewrite may have merged entries from another project. Do not relabel it as this project's fact.
        origin = None
    memory = {key: old_memory.get(key, {"project": origin}) for key, _, _ in memory_entries(context)}
    skills = {key: dict(value) for key, value in index.get("skills", {}).items()}
    for name in changed_skills(result or {}):
        current = skills.get(name, {"projects": []})
        origins = set(current.get("projects", []))
        if project:
            origins.add(project)
        skills[name] = {"projects": sorted(origins)}
    return {"version": 1, "memory": memory, "skills": skills}


def _tokens(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]{3,}", text.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return terms


def context_view(context: dict, project: str | None, query: str = "") -> dict:
    """Do not expose unscoped native/provider memory in global Codex recall."""
    index = context.get("bridge_provenance", {})
    selected = {}
    for key, target, entry in memory_entries(context):
        if project and index.get("memory", {}).get(key, {}).get("project") == project:
            selected.setdefault(target, []).append(entry)
    memories = {key: ENTRY_DELIMITER.join(entries) for key, entries in selected.items()}
    terms = _tokens(query[:4000])
    catalog, ranked = [], []
    for skill in context.get("skills", []):
        origins = index.get("skills", {}).get(skill["name"], {}).get("projects", [])
        score = len(terms & _tokens(skill["name"] + " " + skill.get("description", "")))
        item = {**skill, "source_projects": origins,
                "scope": "shared_skill_with_source_provenance" if origins else "native_catalog"}
        # Cross-project learned skills require a relevant query, or an explicit skill_load.
        if not origins or project in origins or score:
            catalog.append(item)
        if project in origins or score:
            ranked.append((score + (2 if project in origins else 0), item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    recommended = [item["name"] for _, item in ranked[:5]]
    summary = "\n".join(memories.values())[:340]
    if recommended:
        summary += "\n相关技能：" + "、".join(recommended)
    result = {"project": project, "memories": memories, "skills": catalog,
              "recommended_skills": recommended, "summary": summary,
              "recall_policy": "Project memory is scoped; shared skill applicability must be checked against the current task.",
              "provider_recall": "", "unscoped_provider_recall_withheld": bool(context.get("provider_recall"))}
    return {**result, "version": digest(result)}
