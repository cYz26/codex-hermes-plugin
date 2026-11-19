"""Peripheral provenance and recall views; Hermes alone owns memory/skill writes.

Provenance is deliberately conservative. Unknown memory changes (including a
crash between a native write and its receipt) are withheld from automatic recall.
Shared skills remain discoverable by relevance; this is not a tenant ACL.
"""
from __future__ import annotations

import math
import re

from .store import digest

ENTRY_DELIMITER = "\n§\n"  # Pinned Hermes v0.22.7 memory format.
_BOILERPLATE_TOKENS = {"learned", "skill", "skills", "workflow", "method", "procedure"}
_SYNONYM_GROUPS = (
    {"测试", "验证", "单测", "回归", "test", "tests", "testing", "verify", "verification", "validate", "validation"},
    {"修复", "排障", "排查", "故障", "fix", "repair", "debug", "troubleshoot"},
    {"错误", "失败", "异常", "error", "failure", "failed", "exception"},
    {"部署", "上线", "发布", "deploy", "deployment", "release", "publish"},
    {"安装", "初始化", "install", "installation", "setup"},
    {"表头", "分隔", "字段", "header", "headers", "delimited", "csv", "tsv"},
    {"规范化", "清洗", "normalize", "normalization"},
    {"研究", "调研", "分析", "research", "investigation", "analysis"},
    {"资料", "文档", "来源", "source", "sources", "document", "documents", "documentation"},
    {"方案", "选型", "架构", "设计", "alternative", "alternatives", "option", "options", "architecture", "design"},
    {"内部", "企业", "internal", "enterprise"},
    {"外部", "公开", "external", "public"},
    {"能力", "特性", "机制", "capability", "capabilities", "mechanism", "feature"},
    {"比较", "对比", "权衡", "compare", "comparison", "comparative", "tradeoff"},
    {"技术", "工程", "technical", "technology", "engineering"},
    {"配置", "参数", "设置", "config", "configuration", "settings", "model"},
    {"召回", "检索", "发现", "搜索", "recall", "search", "discovery", "lookup"},
    {"通知", "提醒", "回执", "notification", "notifier", "receipt", "notice"},
    {"权限", "授权", "审批", "permission", "authorization", "approval"},
    {"监控", "负载", "指标", "monitoring", "workload", "metrics"},
    {"插件", "钩子", "plugin", "plugins", "hook", "hooks"},
)


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
    present_skills = {item["name"] for item in context.get("skills", [])}
    skills = {key: dict(value) for key, value in index.get("skills", {}).items()
              if "skills" not in context or key in present_skills}
    for name in changed_skills(result or {}):
        current = skills.get(name, {"projects": []})
        origins = set(current.get("projects", []))
        if project:
            origins.add(project)
        skills[name] = {"projects": sorted(origins)}
    return {"version": 1, "memory": memory, "skills": skills}


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    terms = set()
    for word in re.findall(r"[a-z0-9_]{3,}", normalized.lower()):
        terms.add(word)
        for part in word.split("_"):
            if len(part) >= 3:
                terms.add(part)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    for aliases in _SYNONYM_GROUPS:
        if terms & aliases or any(word in text for word in aliases if not word.isascii()):
            terms.update(word for word in aliases if word.isascii())
    return terms


def context_view(context: dict, project: str | None, query: str = "") -> dict:
    """Do not expose unscoped native/provider memory in global Codex recall."""
    index = context.get("bridge_provenance", {})
    selected = {}
    active_boundary=context.get("active_enterprise_boundary")
    for key, target, entry in memory_entries(context):
        required=context.get("enterprise_memory_boundaries",{}).get(key)
        if required and required!=active_boundary:
            continue
        if project and index.get("memory", {}).get(key, {}).get("project") == project:
            selected.setdefault(target, []).append(entry)
    memories = {key: ENTRY_DELIMITER.join(entries) for key, entries in selected.items()}
    terms = _tokens(query[:4000])
    catalog, ranked, raw_eligible = [], [], []
    for skill in context.get("skills", []):
        required=context.get("enterprise_skill_boundaries",{}).get(skill["name"])
        known_publication=context.get("publications",{}).get(skill["name"],{})
        if known_publication.get("audience")=="enterprise":
            required=known_publication.get("boundary_id")
        if required and required!=active_boundary:
            continue
        origins = index.get("skills", {}).get(skill["name"], {}).get("projects", [])
        owns_private = origins == [project]
        if context.get("checked_publication") and not owns_private:
            publication = context.get("publications", {}).get(skill["name"])
            if not publication:
                continue
            if publication.get("audience")=="enterprise" and publication.get("boundary_id")!=active_boundary:
                continue
            skill = publication["catalog_entry"]
        name_tokens = _tokens(skill["name"] + " " + " ".join(skill.get("aliases", []))) - _BOILERPLATE_TOKENS
        desc_tokens = _tokens(skill.get("description", "")) - _BOILERPLATE_TOKENS
        all_tokens = name_tokens | desc_tokens | (_tokens(skill["name"]) & _BOILERPLATE_TOKENS)
        item = {**skill, "source_projects": origins,
                "scope": "shared_skill_with_source_provenance" if origins else "native_catalog"}
        if context.get("checked_publication"):
            item["source_projects"] = [project] if owns_private else []
            item["scope"] = "worktree" if owns_private else "global"
            publication=context.get("publications",{}).get(skill["name"],{})
            if required or publication.get("audience")=="enterprise":
                item.update(audience="enterprise",boundary_id=required or publication.get("boundary_id"))
        raw_eligible.append((item, origins, name_tokens, desc_tokens, all_tokens))

    doc_count = len(raw_eligible)
    doc_freq: dict[str, int] = {}
    for _, _, _, _, doc_tokens in raw_eligible:
        for token in doc_tokens:
            doc_freq[token] = doc_freq.get(token, 0) + 1

    def _idf(token: str) -> float:
        if token in _BOILERPLATE_TOKENS:
            return 0.15
        if doc_count <= 1:
            return 1.0
        return math.log(1.0 + (doc_count + 1.0) / (doc_freq.get(token, 0) + 1.0))

    query_specific = terms - _BOILERPLATE_TOKENS
    effective_terms = query_specific if query_specific else terms
    eligible = []
    for item, origins, name_tokens, desc_tokens, all_tokens in raw_eligible:
        matched_name = effective_terms & name_tokens
        matched_desc = (effective_terms & desc_tokens) - matched_name
        if not matched_name and not matched_desc and not query_specific:
            matched_fallback = terms & all_tokens
        else:
            matched_fallback = set()
        score = (sum(1.5 * _idf(t) for t in matched_name)
                 + sum(_idf(t) for t in matched_desc)
                 + sum(0.2 * _idf(t) for t in matched_fallback))
        eligible.append((item, origins, round(score, 6)))

    # Only eligible endpoints participate, even when an edge points out of scope.
    matched = {item["name"] for item, _, score in eligible if score}
    neighbors = {name for item, _, score in eligible if score for name in item.get("related_skills", [])}
    for item, origins, score in eligible:
        related = item["name"] in neighbors or not matched.isdisjoint(item.get("related_skills", []))
        if not origins or project in origins or score or related:
            catalog.append(item)
        if score or related or (not terms and project in origins):
            origin_bonus = (0.25 if score else 2.0) if project in origins else 0.0
            ranked.append((bool(score), score + origin_bonus, item))
    ranked.sort(key=lambda row: (-int(row[0]), -row[1], row[2]['name']))
    recommended = [item['name'] for _, _, item in ranked[:5]]
    summary = "\n".join(memories.values())[:340]
    if recommended:
        summary += "\n相关技能：" + "、".join(recommended)
    result = {"project": project, "memories": memories, "skills": catalog,
              "recommended_skills": recommended, "summary": summary,
              "recall_policy": "Project memory is scoped; shared skill applicability must be checked against the current task.",
              "provider_recall": "", "unscoped_provider_recall_withheld": bool(context.get("provider_recall"))}
    return {**result, "version": digest(result)}
