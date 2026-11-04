from __future__ import annotations

import subprocess


def message(event: dict) -> str:
    kind = event.get("kind")
    if kind == "review.started":
        return "Hermes 正在整理本轮经验"
    if kind == "skill.loaded":
        return f"本轮已加载 Hermes 技能：{event.get('name', '')}"
    applied, staged = event.get("applied", []), event.get("staged", [])
    if event.get("run_status") in {"failed", "interrupted", "unknown"}:
        return "Hermes 本次学习未完成，已保留执行记录" + (f"；已有 {len(applied)} 项变更生效" if applied else "")
    if applied or staged:
        parts = ([f"Hermes 已学习：{len(applied)} 项变更生效"] if applied else [])
        if staged:
            parts.append(f"{len(staged)} 项修改等待原生批准，尚未生效")
        return "；".join(parts)
    if event.get("refused"):
        return f"Hermes 完成复盘，{len(event['refused'])} 项操作被原生规则拒绝"
    return "Hermes 已完成复盘，本轮没有新增经验"


def macos_notification(text: str) -> dict:
    # Pass strings as argv, never interpolate untrusted learning content into AppleScript.
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
    result = subprocess.run(["/usr/bin/osascript", "-e", script, "Hermes 学习", text],
                            capture_output=True, text=True, timeout=10)
    return {"dispatched": result.returncode == 0, "visibility": "unknown",
            "error": result.stderr.strip() if result.returncode else None}
