"""Codex-specific projection. The Hermes core accepts executor-neutral observations."""
from __future__ import annotations

import json
import re
import time

from .store import Store, digest, dumps


_EXECUTION_TOOLS = {"bash", "exec_command", "write_stdin", "exec", "wait", "terminal"}
BACKGROUND_EXIT_PENDING = "background_exit_pending"
LEARNING_STATUS_TOOLS = {"mcp__hermes_learning__learning_status", "mcp__hermes_learning__feedback_status",
                         "mcp__hermes_learning__record_application"}


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
    if isinstance(decoded, dict) and decoded.get("interrupted") is True:
        return "interrupted", "", None
    if isinstance(decoded, dict) and (decoded.get("isError") is True or decoded.get("error")):
        return "failed", "", None
    if unknown:
        return "unknown", "", "unrecognized_execution_result"
    if isinstance(decoded, dict):
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


def settle_turn(data: dict) -> None:
    ended_by = data.get("_ended_by") or ("Stop" if data["status"] == "incomplete" and data.get("assistant") else None)
    pending = [tool for tool in data["tools"].values() if tool["status"] in {"running", "unknown"}]
    background = [tool for tool in pending if tool["status"] == "running" and tool.get("background_session")]
    coverage = set(data.get("coverage", [])) - {BACKGROUND_EXIT_PENDING}
    if background:
        coverage.add(BACKGROUND_EXIT_PENDING)
    data["coverage"] = sorted(coverage)
    if ended_by == "Interrupt":
        data["status"] = "interrupted"
    elif ended_by == "Stop":
        unresolved = len(pending) != len(background) or (background and "missing_tool_use_id" in coverage)
        data["status"] = "incomplete" if unresolved else "completed"
        if data.get('_evidence_mode') == 'workflow-v1':
            # The lifecycle ended; unknown tool outcomes remain explicitly unknown.
            data['status'] = 'completed'


def _background_poll_targets(data, event):
    if event.get("tool_name", "").rsplit(".", 1)[-1] != "write_stdin":
        return []
    args = event.get("tool_input")
    session = args.get("session_id") if isinstance(args, dict) else None
    if type(session) not in {str, int}:
        return []
    transcript = digest(event.get("transcript_path"))
    return [tool for tool in data["tools"].values()
            if (background := tool.get("background_session")) and background["session_id"] == str(session)
            and background["transcript_ref"] == transcript]


def merge_event(store: Store, row: dict) -> dict | None:
    event = json.loads(row["payload"])
    name = event.get("hook_event_name")
    session, turn = event.get("session_id"), event.get("turn_id")
    if not session or not turn:
        return None
    key = digest([session, turn])
    previous = store.db.execute("SELECT payload,revision,observed_revision FROM turns WHERE id=?", (key,)).fetchone()
    data = json.loads(previous[0]) if previous else {
        "source": {"host": event.get("bridge_host", "codex-desktop"),
                   "project": event.get("bridge_project", event["cwd"]), "chat": session, "turn": turn,
                   "executor": "Codex App"},
        "user": "", "assistant": "", "tools": {}, "status": "running", "coverage": [],
    }
    if previous and (event.get("bridge_host", "codex-desktop") != data["source"]["host"]
                     or event.get("bridge_project", event["cwd"]) != data["source"]["project"]):
        return None
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
        poll_targets = _background_poll_targets(data, event)
        if len(poll_targets) == 1:
            return None  # A same-session poll retrieves evidence for the original call.
        data["status"] = "running"
        tool_id = event.get("tool_use_id")
        if tool_id:
            data["tools"].setdefault(tool_id, {
                "call_id": tool_id, "name": event.get("tool_name", "unknown"),
                "arguments": event.get("tool_input") or {}, "result": None, "status": "running", "process": "",
                "action": event.get("bridge_action"), "test_key": event.get("bridge_test_key"),
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
            matches = [t for t in data["tools"].values() if t.get("process") and str(t["process"]) == str(polled)
                       and not t.get("background_session")]
            poll_targets = _background_poll_targets(data, event)
            if len(poll_targets) == 1 and not matches and poll_targets[0]["background_session"].get("exit_event"):
                settled = poll_targets[0]["background_session"]
                if settled["exit_event"] == row["id"] and previous["revision"] != previous["observed_revision"]:
                    return data
                data["_skip_observation"] = row["id"]
                return data
            matches.extend(poll_targets)
            original = matches[0] if len(matches) == 1 else None
            target_id = original["call_id"] if original else tool_id
            if original and tool_id != target_id:
                data["tools"].pop(tool_id, None)
            previous_call = data["tools"].get(target_id, {})
            background = previous_call.get("background_session")
            if background:
                if background.get("exit_event"):
                    if background["exit_event"] == row["id"] and previous["revision"] != previous["observed_revision"]:
                        return data  # This exact exit was merged but not yet observed.
                    data["_skip_observation"] = row["id"]
                    return data  # A later result cannot revise or recount a retained exit.
                if (digest(event.get("transcript_path")) != background["transcript_ref"] or
                        (not original and (tool_name != previous_call["name"] or arguments != previous_call["arguments"]))):
                    data["_skip_observation"] = row["id"]
                    return data
                decoded, unknown = _execution_result(response)
                has_exit_code = not unknown and isinstance(decoded, dict) and type(decoded.get("exit_code")) is int
                terminal = status == "interrupted" or (has_exit_code and status ==
                            ("completed" if decoded["exit_code"] == 0 else "failed"))
                if status not in {"completed", "failed", "interrupted"} or not terminal:
                    # Preserve the confirmed launch. A same-call Hook may arrive
                    # before its structured host result; keep its identity to join later.
                    background["pending_exit_event"] = row["id"]
                    status, process, response, gap = "running", previous_call["process"], None, None
                else:
                    background = {k: v for k, v in background.items() if k != "pending_exit_event"}
                    background["exit_event"] = row["id"]
            tool_action, test_key = event.get("bridge_action"), event.get("bridge_test_key")
            if original:
                arguments = original["arguments"]
                tool_action, test_key = original.get("action"), original.get("test_key")
            elif not arguments:
                arguments = previous_call.get("arguments") or arguments
                tool_action = previous_call.get("action") or tool_action
                test_key = previous_call.get("test_key") or test_key
            data["tools"][target_id] = {
                "call_id": target_id, "name": original["name"] if original else tool_name,
                "arguments": arguments,
                "result": response, "status": status, "process": process,
                "action": tool_action, "test_key": test_key,
                **({"background_session": background} if background else {}),
            }
            if gap:
                data["coverage"] = sorted(set(data["coverage"]) | {gap + ":" + target_id})
    elif name == "Stop":
        if data.get("_ended_by") == "Stop" and any(tool.get("background_session") for tool in data["tools"].values()):
            data["_skip_observation"] = row["id"]
            return data
        data["assistant"] = event.get("last_assistant_message", "")
        provenance.update(terminal_event=row["id"], terminal_at=captured_at)
        if data.get("_ended_by") != "Interrupt" and data["status"] != "interrupted":
            data["_ended_by"] = "Stop"
    elif name == "Interrupt":
        data["_ended_by"] = "Interrupt"
    settle_turn(data)
    revision = digest(data)
    now = time.time()
    stopped = now if data["status"] == "completed" else None
    with store.db:
        store.db.execute("INSERT INTO turns(id,payload,revision,stopped,activity) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,"
                         "stopped=excluded.stopped,activity=excluded.activity", (key, dumps(data), revision, stopped, now))
        store.db.execute("INSERT OR REPLACE INTO latest_turns VALUES (?,?,?)", (session, key, now))
    return data


def compact_learning_result(response):
    from .delegation import _result_object
    value = _result_object(response)
    if value is None:
        value = response if isinstance(response, dict) and response.get("retained_detail") == "learning_status_metadata_only" else {}
    build = value.get("build")
    result = {key: value[key] for key in ("id", "state", "name", "version", "validation", "validation_reason", "build_digest")
              if isinstance(value.get(key), str)}
    if isinstance(build, dict) and isinstance(build.get("digest"), str):
        result["build_digest"] = build["digest"]
    result["retained_detail"] = "learning_status_metadata_only"
    return result


def observation(data: dict) -> dict:
    tools = []
    for tool in data["tools"].values():
        name = tool.get("name")
        if name in LEARNING_STATUS_TOOLS:
            arguments = tool.get("arguments")
            args = {key: val for key, val in arguments.items() if key != "delivery_token"} if isinstance(arguments, dict) else {}
            tool = {**tool, "arguments": args, "result": compact_learning_result(tool.get("result"))}
        tools.append(tool)
    return {**{key: value for key, value in data.items() if not key.startswith("_") and key != "task_input_provenance"},
            "tools": tools}
