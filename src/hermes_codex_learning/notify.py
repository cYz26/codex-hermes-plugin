from __future__ import annotations

import plistlib
import subprocess
import time
from pathlib import Path

from .approvals import pending_proposal_id
from .review_input import BUDGET_EXHAUSTED
from .review_receipt import receipt_text
from .review_summary import summary_text
from .store import atomic_json, digest
from .macos_bundle import (APP_NAME, APP_ID, APP_EXECUTABLE, LEGACY_APP_ID,
                           bundle_identifier, legacy_notifier_app, notifier_app)

APPROVAL_REQUIRED = "approval.required"


def approval_notice(settings, event: dict) -> dict | None:
    project = event.get("project")
    if (event["kind"] != "review.finished" or not event.get("staged")
            or not project or settings.project_for(project) != project):
        return None

    proposals = {"memory": set(), "user": set(), "skills": set()}
    for fact in event["staged"]:
        identifier = pending_proposal_id(fact)
        if not identifier or fact.get("tool") not in ("memory", "skill_manage"):
            continue
        subsystem = "memory" if fact["tool"] == "memory" else "skills"
        path = settings.home / "pending" / subsystem / (identifier + ".json")
        if path.is_symlink() or not path.is_file():
            continue
        target = "user" if subsystem == "memory" and fact.get("target") == "user" else subsystem
        proposals[target].add(identifier)

    counts, details, entities = [], [], []
    for target, label, instruction in (
        ("memory", "记忆", "请在项目审批页查看差异。"),
        ("user", "用户档案", "请在学习实例的 Hermes 中运行 /memory pending。"),
        ("skills", "技能", "请在学习实例的 Hermes 中运行 /skills pending。"),
    ):
        identifiers = sorted(proposals[target])
        if identifiers:
            counts.append(f"{len(identifiers)} 项{label}提案")
            details.append(f"{label}提案 {'、'.join(identifiers)}。{instruction}")
            entities.extend(f"{target}:{identifier}" for identifier in identifiers)
    if not entities:
        return None

    summary = f"[{Path(project).name or project}] {'、'.join(counts)}待审批"
    return {"id": event["id"] + ":approval", "kind": APPROVAL_REQUIRED, "project": project,
            "message": summary, "receipt": "\n\n".join([summary, *details]),
            "pending_entities": entities, "project_approval": bool(proposals["memory"])}


def should_notify(event: dict) -> bool:
    if event["kind"] in {APPROVAL_REQUIRED, "update.notice", "publication.applied", "publication.pending", "review.retry_scheduled"}:
        return True
    return event["kind"] == "review.finished" and bool(
        event.get("applied_entities", event.get("applied")) or event.get("run_status") != "completed"
        or (event.get("refused") if event.get("approval_notice") else event.get("staged"))
        or (event.get("retry") or {}).get("retry_index")
    )


def notification_events(event: dict):
    if reminder := event.get("approval_notice"):
        yield reminder
    if should_notify(event):
        yield event


def message(event: dict) -> str:
    text = _message(event)
    from .review_retries import retry_text
    recovery = retry_text(event.get("retry"))
    if recovery:
        text += "；" + recovery
    if event.get("kind") == "review.finished":
        summary = summary_text(event.get("review_summary"))
        if summary:
            preview = " ".join(summary.split())
            text += "\n" + (preview if len(preview) <= 100 else preview[:99].rstrip() + "…")
    return f"[{Path(event['project']).name}] {text}" if event.get("project") else text


def _message(event: dict) -> str:
    kind = event.get("kind")
    if kind == "publication.applied":
        return "Hermes 已发布 1 项通用技能：" + event["name"]
    if kind == "publication.pending":
        return "Hermes 有 1 项通用候选待验证，尚未共享：" + event["name"]
    if kind == "review.started":
        return f"Hermes 正在第 {event['retry_index']} 次重试复盘" if event.get("retry_index") else "Hermes 正在整理本轮经验"
    if kind == "review.retry_scheduled":
        return "Hermes 已保留上次复盘记录"
    if kind == "skill.loaded":
        return f"本轮已加载 Hermes 技能：{event.get('name', '')}"
    from .notices import entities
    applied = event.get("applied_entities", entities(event.get("applied", [])))
    staged = [] if event.get("approval_notice") else entities(event.get("staged", []))
    if event.get("run_status") in {"failed", "interrupted", "unknown"}:
        reason = ("Hermes 复盘输入超出预算" if event.get("failure_reason") == BUDGET_EXHAUSTED
                  else "Hermes 本次学习未完成")
        return f"{reason}，已保留执行记录" + (f"，已有 {len(applied)} 项变更生效" if applied else "")
    if applied or staged:
        parts = ([f"Hermes 已学习：{len(applied)} 项变更生效"] if applied else [])
        if staged:
            parts.append(f"{len(staged)} 项修改等待原生批准，尚未生效")
        if event.get("refused"):
            parts.append(f"{len(event['refused'])} 次操作尝试被拒绝")
        return "；".join(parts)
    if event.get("refused"):
        return f"Hermes 完成复盘，{len(event['refused'])} 次操作尝试被拒绝"
    return "Hermes 已完成复盘，本轮没有新增经验"


def persist_receipt(state: Path, event: dict) -> Path:
    path = state / "receipts" / (digest(event["id"]) + ".json")
    if not path.exists():
        detail = event.get("receipt") or receipt_text(event, {}, event.get("created", time.time()))
        receipt = {"id": event["id"], "scope": event.get("scope") or event.get("project") or "global",
                   "summary": event["message"], "text": detail}
        if event.get("kind") == APPROVAL_REQUIRED:
            receipt.update(kind=APPROVAL_REQUIRED, project_approval=event["project_approval"])
        atomic_json(path, receipt)
    return path


def macos_notification(state: Path, event: dict) -> dict:
    path = persist_receipt(state, event)
    record = {"dispatched": False, "visibility": "unknown", "transport": APP_ID}
    try:
        record["receipt_path"] = str(path)
        app = notifier_app()
        if not app.exists():
            legacy = legacy_notifier_app(app)
            if bundle_identifier(legacy) == LEGACY_APP_ID:
                app = legacy
        record["transport"] = bundle_identifier(app) or APP_ID
        binary = app / "Contents" / "MacOS" / APP_EXECUTABLE
        result = subprocess.run([str(binary), "--post", str(path)], capture_output=True, text=True, timeout=10)
        return {**record, "dispatched": result.returncode == 0,
                "error": (result.stderr.strip() or f"Notifier exited with {result.returncode}") if result.returncode else None}
    except (OSError, plistlib.InvalidFileException, subprocess.TimeoutExpired) as exc:
        return {**record, "error": str(exc)}
