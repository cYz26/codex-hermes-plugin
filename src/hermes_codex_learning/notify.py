from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .review_input import BUDGET_EXHAUSTED
from .review_receipt import receipt_text
from .store import atomic_json, digest

APP_NAME = "Hermes 学习"
APP_ID = "com.cy.hermes-learning-notifier"
APP_EXECUTABLE = "HermesLearningNotifier"


def notifier_app() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def should_notify(event: dict) -> bool:
    if event["kind"] == "update.notice":
        return True
    return event["kind"] == "review.finished" and bool(
        event.get("applied") or event.get("staged") or event.get("run_status") != "completed"
    )


def message(event: dict) -> str:
    text = _message(event)
    return f"[{Path(event['project']).name}] {text}" if event.get("project") else text


def _message(event: dict) -> str:
    kind = event.get("kind")
    if kind == "review.started":
        return "Hermes 正在整理本轮经验"
    if kind == "skill.loaded":
        return f"本轮已加载 Hermes 技能：{event.get('name', '')}"
    applied, staged = event.get("applied", []), event.get("staged", [])
    if event.get("run_status") in {"failed", "interrupted", "unknown"}:
        reason = ("Hermes 复盘输入超出预算" if event.get("failure_reason") == BUDGET_EXHAUSTED
                  else "Hermes 本次学习未完成")
        return f"{reason}，已保留执行记录" + (f"，已有 {len(applied)} 项变更生效" if applied else "")
    if applied or staged:
        parts = ([f"Hermes 已学习：{len(applied)} 项变更生效"] if applied else [])
        if staged:
            parts.append(f"{len(staged)} 项修改等待原生批准，尚未生效")
        return "；".join(parts)
    if event.get("refused"):
        return f"Hermes 完成复盘，{len(event['refused'])} 项操作被原生规则拒绝"
    return "Hermes 已完成复盘，本轮没有新增经验"


def macos_notification(state: Path, event: dict) -> dict:
    path = state / "receipts" / (digest(event["id"]) + ".json")
    record = {"dispatched": False, "visibility": "unknown", "transport": APP_ID}
    try:
        if not path.exists():
            detail = event.get("receipt") or receipt_text(event, {}, event.get("created", time.time()))
            atomic_json(path, {"summary": event["message"], "text": detail})
        record["receipt_path"] = str(path)
        binary = notifier_app() / "Contents" / "MacOS" / APP_EXECUTABLE
        result = subprocess.run([str(binary), "--post", str(path)], capture_output=True, text=True, timeout=10)
        return {**record, "dispatched": result.returncode == 0,
                "error": (result.stderr.strip() or f"Notifier exited with {result.returncode}") if result.returncode else None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**record, "error": str(exc)}
