"""Derive desktop presentation from the recorded learning outcome."""
from __future__ import annotations

import math
import re
from pathlib import Path

from .enterprise import scrub
from .review_deadline import REVIEW_DEADLINE
from .review_input import BUDGET_EXHAUSTED
from .review_summary import summary_text


RETRY_REASONS = {
    "rate_limit": "模型限流",
    "connection": "连接暂时中断",
    "provider_unavailable": "模型服务暂时不可用",
    "interrupted": "原复盘中断",
    "unconfirmed_outcome": "执行结果待核对",
    "non_transient": "错误需要人工核对",
    REVIEW_DEADLINE: "本轮复盘达到总时限",
}
WAITING_FOR_CHECK = {
    None, "unknown", "data_classification_unassessed", "executable_resource_needs_validation",
    "independent_suite_required", "unknown_suite_or_entry", "verified_sandbox_unavailable",
}


def brief(text: str, limit=64) -> str:
    compact = " ".join(scrub(text).split())
    return compact if len(compact) <= limit else compact[:limit - 1].rstrip() + "…"


def section(title: str, text: str) -> dict:
    return {"title": title, "text": text}


def retry_presentation(retry: dict) -> tuple[str, str, str, float | None]:
    state = retry.get("state")
    reason = RETRY_REASONS.get(retry.get("reason"), "执行记录已保留")
    maximum = retry.get("max_retries")
    if state in {"waiting", "queued"}:
        attempt = retry.get("next_retry_index")
        if type(attempt) is int and type(maximum) is int:
            reason += f" · 第 {attempt}/{maximum} 次"
        when = retry.get("next_retry_at")
        due = when if state == "waiting" and type(when) in (int, float) and 0 < when < 253402300800 and math.isfinite(when) else None
        return "已安排自动重试", reason, "waiting", due
    if state == "exhausted":
        detail = f"{reason} · 已重试 {maximum} 次" if type(maximum) is int else reason
        return "已达到重试上限", detail, "error", None
    if state == "blocked":
        return "自动重试已暂停", reason, "warning", None
    if state == "disabled":
        return "自动重试已关闭", reason, "warning", None
    if state == "completed" and retry.get("retry_index"):
        return "重试完成", f"第 {retry['retry_index']} 次重试完成", "success", None
    return "", "", "info", None


def review_presentation(event: dict) -> dict:
    from .notices import entities

    if "applied_entities" in event:
        applied = sorted(set(event["applied_entities"]))
    else:
        applied = entities(event.get("applied", []))
    staged = event.get("staged") or []
    refused = event.get("refused") or []
    uncertain = event.get("uncertain") or []
    status = event.get("run_status")
    summary = summary_text(event.get("review_summary")) if status == "completed" else None
    retry_title, retry_body, retry_tone, retry_at = retry_presentation(event.get("retry") or {})
    applied_title = f"已生效 {len(applied)} 项"
    sections = []
    if summary:
        sections.append(section("学习结论", summary))
    if applied:
        skills = ["技能 " + brief(item.removeprefix("skill:"), 120) for item in applied if item.startswith("skill:")]
        memories = sum(item.startswith(("memory:", "memory-target:")) for item in applied)
        changes = [applied_title, *skills, *([f"记忆 · {memories} 项"] if memories else [])]
        sections.append(section("已生效变更", "\n".join(changes)))
    if staged:
        pending = (event.get("approval_notice") or {}).get("pending_entities", [])
        count = f"{len(pending)} 项提案" if pending else f"{len(staged)} 组修改"
        sections.append(section("待审批", count + "待处理，尚未生效。"))

    issues = []
    if status in {"failed", "interrupted", "unknown"} or (event["kind"] == "review.finished" and status != "completed"):
        outcome = {"failed": "复盘失败", "interrupted": "复盘中断"}.get(status, "复盘结果待确认")
        if event.get("failure_reason") == BUDGET_EXHAUSTED:
            outcome = "复盘输入超出预算"
        issues.append(outcome + "，执行记录已保留。")
        title = retry_title or outcome
        tone = retry_tone if retry_title else "error" if status == "failed" else "warning"
        if applied:
            title, tone = f"{outcome} · {len(applied)} 项已生效", "warning"
        body = retry_body or "执行记录已保留"
    elif event["kind"] == "review.retry_scheduled":
        title, body, tone = retry_title or "复盘恢复记录", retry_body or "执行记录已保留", retry_tone
    else:
        title = applied_title if applied else retry_title or "复盘完成"
        tone = "success" if applied else "info"
        sentence = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", summary, maxsplit=1)[0] if summary else ""
        body = brief(sentence)
    if refused:
        issues.append(f"{len(refused)} 次操作尝试被 Hermes 拒绝。")
    if uncertain:
        issues.append(f"{len(uncertain)} 项执行结果待核对。")
    if retry_title:
        issues.append(" · ".join(dict.fromkeys((retry_title, retry_body))))
    if issues:
        sections.append(section("异常与重试", "\n".join(issues)))
        if not body:
            body = issues[0]
        if tone in {"info", "success"} and (refused or uncertain):
            tone = "warning"
    if not body and not applied:
        body = "有修改待审批" if staged else "本轮没有新增经验"
    result = {"title": title, "body": body, "tone": tone, "sections": sections}
    if retry_at is not None:
        result["retry_at"] = retry_at
    return result


def notice_presentation(event: dict) -> dict | None:
    kind = event["kind"]
    project = event.get("project")
    subtitle = brief(Path(project).name, 48) if project else ""
    if kind in {"review.finished", "review.retry_scheduled"}:
        return {**review_presentation(event), "subtitle": subtitle}

    name = brief(event.get("name") or "", 120)
    if kind == "approval.required":
        shared = event.get("approval_subject") == "publication"
        title = "⚠️ 全局待审批" if shared else "⚠️ 项目待审批"
        count = len(event.get("pending_entities", []))
        body = name if shared and name else (f"{count} 项提案待处理" if count else "有提案待处理")
        instruction = ("请核对待审版本的文件差异。" if shared else "请核对项目审批页中的修改。"
                       if event.get("project_approval") else "打开明细查看原生待审入口。")
        sections = [section("待审批", body + "\n" + instruction)]
        tone = "warning"
    elif kind == "publication.applied":
        title, body, tone = "技能已共享", name, "success"
        sections = [section("已生效变更", f"{name}\n可供其他项目使用。")]
    elif kind == "publication.pending":
        reason = event.get("publication_reason")
        waiting = reason in WAITING_FOR_CHECK
        title, body, tone = ("候选待检查" if waiting else "共享检查受阻"), name, "waiting" if waiting else "warning"
        if waiting:
            detail = "等待发布检查，当前尚未共享。"
        elif reason == "missing_applicability_limits_or_verification":
            labels = {"applicability": "适用场景", "limits": "限制/边界", "verification": "验证方法"}
            missing = [labels.get(item, str(item)) for item in event.get("missing_sections") or []]
            suffix = f"（缺少：{'、'.join(missing)}）" if missing else "（缺少适用场景、限制/边界或验证方法）"
            detail = f"SKILL.md 未满足发布内容检查{suffix}，当前尚未共享。"
        else:
            detail = "本版本未通过发布检查，详见完整记录。"
        sections = [section("共享检查" if waiting else "异常与重试", f"{name}\n{detail}")]
    else:
        return None
    return {"title": title, "subtitle": subtitle, "body": brief(body), "tone": tone, "sections": sections}
