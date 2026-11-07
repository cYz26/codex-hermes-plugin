"""Codex-specific projection. The Hermes core accepts executor-neutral observations."""
from __future__ import annotations

import json
import re
import time

from .store import Store, digest, dumps


def merge_event(store: Store, row: dict) -> dict | None:
    event = json.loads(row["payload"])
    name = event.get("hook_event_name")
    session, turn = event.get("session_id"), event.get("turn_id")
    if not session or not turn:
        return None
    key = digest([session, turn])
    previous = store.db.execute("SELECT payload FROM turns WHERE id=?", (key,)).fetchone()
    data = json.loads(previous[0]) if previous else {
        "source": {"host": event.get("bridge_host", "codex-desktop"),
                   "project": event.get("bridge_project", event["cwd"]), "chat": session, "turn": turn,
                   "executor": "Codex App"},
        "user": "", "assistant": "", "tools": {}, "status": "running", "coverage": [],
    }
    if name == "UserPromptSubmit":
        data["user"] = event.get("prompt", "")
        data["status"] = "running"
    elif name == "PreToolUse":
        data["status"] = "running"
    elif name == "PostToolUse":
        tool_id = event.get("tool_use_id")
        if not tool_id:
            data["coverage"] = list(set(data["coverage"]) | {"missing_tool_use_id"})
        else:
            response = event.get("tool_response", {})
            text = response if isinstance(response, str) else dumps(response)
            # A terminal request returning a process session is not yet a completed command.
            process = response.get("session_id") if isinstance(response, dict) else None
            match = re.search(r"Process running with session ID (\d+)", text)
            process = str(process or (match[1] if match else ""))
            arguments = event.get("tool_input") or {}
            original = next((t for t in data["tools"].values()
                             if t.get("process") and str(t["process"]) == str(arguments.get("session_id", ""))), None)
            target_id = original["call_id"] if original else tool_id
            data["tools"][target_id] = {
                "call_id": target_id, "name": original["name"] if original else event["tool_name"],
                "arguments": original["arguments"] if original else arguments,
                "result": response, "status": "running" if process else "completed", "process": process,
            }
    elif name == "Stop":
        data["assistant"] = event.get("last_assistant_message", "")
        data["status"] = "completed"
        if any(t["status"] == "running" for t in data["tools"].values()):
            data["status"] = "incomplete"
    elif name == "Interrupt":
        data["status"] = "interrupted"
    # A late terminal result can complete a previously stopped observation.
    if name == "PostToolUse" and data["status"] == "incomplete" and data["assistant"]:
        if all(t["status"] == "completed" for t in data["tools"].values()):
            data["status"] = "completed"
    revision = digest(data)
    now = time.time()
    stopped = now if data["status"] == "completed" else None
    with store.db:
        store.db.execute("INSERT INTO turns(id,payload,revision,stopped,activity) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,"
                         "stopped=excluded.stopped,activity=excluded.activity", (key, dumps(data), revision, stopped, now))
    return data


def observation(data: dict) -> dict:
    # The neutral API receives the provenance in user/coverage; the full join stays in the bridge.
    return {**{key: value for key, value in data.items() if key != "task_input_provenance"},
            "tools": list(data["tools"].values())}
