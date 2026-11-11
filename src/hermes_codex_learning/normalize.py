"""Codex-specific projection. The Hermes core accepts executor-neutral observations."""
from __future__ import annotations

import json
import re
import time

from .store import Store, digest, dumps


_EXECUTION_TOOLS = {"bash", "exec_command", "write_stdin", "exec", "wait", "terminal"}


def _execution_result(response):
    """Decode one known MCP text/structured envelope, never arbitrary nested text."""
    if not isinstance(response, dict) or "content" not in response:
        return response, False
    structured = response.get("structuredContent")
    if isinstance(structured, dict) and any(key in structured for key in ("session_id", "exit_code", "cell_id", "error", "isError")):
        return structured, False
    content = response["content"]
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        block = content[0]
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                decoded = json.loads(block["text"])
            except ValueError:
                decoded = None
            if isinstance(decoded, dict) and any(key in decoded for key in ("session_id", "exit_code", "cell_id")):
                return decoded, False
    return response, True


def _result_state(name: str, response) -> tuple[str, str, str | None]:
    execution = name.rsplit(".", 1)[-1].lower() in _EXECUTION_TOOLS
    decoded, unknown = _execution_result(response) if execution else (response, False)
    if isinstance(decoded, dict) and (decoded.get("isError") is True or decoded.get("error")):
        return "failed", "", None
    if unknown:
        return "unknown", "", "unrecognized_execution_result"
    if isinstance(decoded, dict):
        if decoded.get("interrupted") is True:
            return "interrupted", "", None
        if execution and type(decoded.get("exit_code")) is int:
            return ("completed" if decoded["exit_code"] == 0 else "failed"), "", None
        process = None
        if execution:
            process = decoded.get("session_id")
            if process is None:
                process = decoded.get("cell_id")
        if process is not None:
            return "running", str(process), None
        if execution:
            return "unknown", "", "unrecognized_execution_result"
    if execution and isinstance(decoded, str):
        match = re.search(r"^Process running with session ID (\d+)\s*$", decoded, re.M)
        if match:
            return "running", match[1], None
    if execution:
        return "unknown", "", "unrecognized_execution_result"
    return "completed", "", None


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
    provenance = data.setdefault("evidence_provenance", {})
    captured_at = row["created"] if "created" in row.keys() else time.time()
    provenance.setdefault("first_captured_at", captured_at)
    if event.get("bridge_host_context"):
        data["host_context"] = event["bridge_host_context"]
    if event.get('bridge_evidence_mode') == 'workflow-v1':
        if data.get('_evidence_mode') != 'semantic-v2':
            data['_evidence_mode'] = 'workflow-v1'
        from .workflow import USER
        data['user'] = data.get('user') or USER
    elif event.get('bridge_evidence_mode') == 'semantic-v2':
        data['_evidence_mode'] = 'semantic-v2'
    if name == "UserPromptSubmit":
        data["user"] = event.get("prompt", "")
        provenance.update(request_event=row["id"], request_at=captured_at,
                          body_retained=event.get("bridge_admission", {}).get("body_retained") is not False)
        # A real request has a distinct turn id. A late receipt for this same
        # request cannot erase an already received Stop/Interrupt.
        if not data.get("_ended_by"):
            data["status"] = "running"
    elif name == "PreToolUse":
        data["status"] = "running"
        tool_id = event.get("tool_use_id")
        if tool_id:
            data["tools"].setdefault(tool_id, {
                "call_id": tool_id, "name": event.get("tool_name", "unknown"),
                "arguments": event.get("tool_input") or {}, "result": None, "status": "running", "process": "",
            })
        else:
            data["coverage"] = sorted(set(data["coverage"]) | {"missing_tool_use_id"})
    elif name == "PostToolUse":
        tool_id = event.get("tool_use_id")
        if not tool_id:
            data["coverage"] = list(set(data["coverage"]) | {"missing_tool_use_id"})
        else:
            response = event.get("tool_response", {})
            tool_name = event.get("tool_name", "unknown")
            status, process, gap = _result_state(tool_name, response)
            if event.get('bridge_evidence_mode') == 'workflow-v1':
                from .workflow import OUTCOMES
                status = response.get('outcome', 'unknown')
                status = status if status in OUTCOMES else 'unknown'
                process, gap = event.get('bridge_process', ''), None
            arguments = event.get("tool_input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
                data["coverage"] = sorted(set(data["coverage"]) | {"unrecognized_tool_input:" + tool_id})
            polled = event.get("bridge_polled") or arguments.get("session_id") or arguments.get("cell_id")
            original = next((t for t in data["tools"].values()
                             if t.get("process") and str(t["process"]) == str(polled)), None)
            target_id = original["call_id"] if original else tool_id
            if original and tool_id != target_id:
                data["tools"].pop(tool_id, None)
            data["tools"][target_id] = {
                "call_id": target_id, "name": original["name"] if original else tool_name,
                "arguments": original["arguments"] if original else arguments,
                "result": response, "status": status, "process": process,
                "action": original.get("action") if original else event.get("bridge_action"),
                "test_key": original.get("test_key") if original else event.get("bridge_test_key"),
            }
            if gap:
                data["coverage"] = sorted(set(data["coverage"]) | {gap + ":" + target_id})
    elif name == "Stop":
        data["assistant"] = event.get("last_assistant_message", "")
        provenance.update(terminal_event=row["id"], terminal_at=captured_at)
        if data.get("_ended_by") != "Interrupt" and data["status"] != "interrupted":
            data["_ended_by"] = "Stop"
    elif name == "Interrupt":
        data["_ended_by"] = "Interrupt"
    # Migrate old persisted incomplete turns without changing the native API shape.
    ended_by = data.get("_ended_by") or ("Stop" if data["status"] == "incomplete" and data["assistant"] else None)
    if ended_by == "Interrupt":
        data["status"] = "interrupted"
    elif ended_by == "Stop":
        pending = any(t["status"] in {"running", "unknown"} for t in data["tools"].values())
        data["status"] = "incomplete" if pending else "completed"
        if data.get('_evidence_mode') == 'workflow-v1':
            # The lifecycle ended; unknown tool outcomes remain explicitly unknown.
            data['status'] = 'completed'
    revision = digest(data)
    now = time.time()
    stopped = now if data["status"] == "completed" else None
    with store.db:
        store.db.execute("INSERT INTO turns(id,payload,revision,stopped,activity) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,"
                         "stopped=excluded.stopped,activity=excluded.activity", (key, dumps(data), revision, stopped, now))
        store.db.execute("INSERT OR REPLACE INTO latest_turns VALUES (?,?,?)", (session, key, now))
    return data


def observation(data: dict) -> dict:
    return {**{key: value for key, value in data.items() if not key.startswith("_") and key != "task_input_provenance"},
            "tools": list(data["tools"].values())}
