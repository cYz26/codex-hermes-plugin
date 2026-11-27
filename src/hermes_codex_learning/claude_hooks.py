"""Translate Claude Code command hooks without reading its private transcripts."""
from __future__ import annotations

import json
import re

EXECUTOR = "Claude Code"
STOP_FAILURE = "StopFailure"
POST_TOOL_SUCCESS = "claude_post_tool_use"
SHELL_FAILURE_EXIT = "claude_shell_failure_exit"
MISSING_STOP = "claude_stop_unavailable"
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
HOOK_EVENTS = TOOL_EVENTS | {"SessionStart", "UserPromptSubmit", "Stop", STOP_FAILURE, "SessionEnd"}
PLUGIN_MCP_PREFIX = "mcp__plugin_hermes-learning_hermes_learning__"
_SHELL_TOOLS = {"Bash", "PowerShell"}
_EXIT_LINE = re.compile(r"Exit code (-?[0-9]{1,9})")


def adapt(payload: dict) -> tuple[dict, dict]:
    """Return the admission payload and independently observed host facts."""
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    if (not isinstance(event, str) or event not in HOOK_EVENTS
            or not isinstance(session, str) or not 0 < len(session) <= 256):
        return {}, {}
    body = {"hook_event_name": event, "session_id": session, "cwd": payload.get("cwd")}
    facts = {"bridge_executor": EXECUTOR}
    prompt_id = payload.get("prompt_id")
    if event not in {"SessionStart", "SessionEnd"} and isinstance(prompt_id, str) and 0 < len(prompt_id) <= 256:
        body["turn_id"] = prompt_id
    # Older clients can still recall session context. Without the prompt ID,
    # retain lifecycle metadata only and never guess which turn owns tool text.
    if "turn_id" not in body:
        return body, facts
    if event == "UserPromptSubmit" and isinstance(payload.get("prompt"), str):
        body["prompt"] = payload["prompt"]
    elif event == "Stop":
        if isinstance(payload.get("last_assistant_message"), str):
            body["last_assistant_message"] = payload["last_assistant_message"]
        if payload.get("background_tasks"):
            facts["bridge_background_pending"] = True
        if payload.get("stop_hook_active") is True:
            facts["bridge_stop_continuation"] = True
    elif event in TOOL_EVENTS:
        tool, call = payload.get("tool_name"), payload.get("tool_use_id")
        if not isinstance(tool, str) or not 0 < len(tool) <= 256:
            return {}, {}
        if isinstance(call, str) and 0 < len(call) <= 256:
            body["tool_use_id"] = call
        if tool.startswith(PLUGIN_MCP_PREFIX):
            tool = "mcp__hermes_learning__" + tool.removeprefix(PLUGIN_MCP_PREFIX)
        body["tool_name"] = tool
        arguments = payload.get("tool_input")
        body["tool_input"] = arguments if isinstance(arguments, dict) else {}
        if event == "PostToolUseFailure":
            body["hook_event_name"] = "PostToolUse"
            error = payload.get("error")
            error = error if isinstance(error, str) else "tool_execution_failed"
            interrupted = payload.get("is_interrupt") is True
            response = {"error": error, "interrupted": interrupted}
            line = error.partition("\n")[0].rstrip("\r")
            if tool in _SHELL_TOOLS and (match := _EXIT_LINE.fullmatch(line)):
                code = int(match[1])
                if code != 0 and not interrupted:
                    response["exit_code"] = code
                    facts["bridge_tool_status_source"] = SHELL_FAILURE_EXIT
            body["tool_response"] = response
            facts["bridge_tool_status"] = "interrupted" if interrupted else "failed"
        elif event == "PostToolUse":
            response = payload.get("tool_response")
            body["tool_response"] = _mcp_response(response) if tool.startswith("mcp__") else response
            if tool in _SHELL_TOOLS:
                status = _shell_status(body["tool_input"], response)
                facts["bridge_tool_status"] = status
                if status == "completed":
                    facts["bridge_tool_status_source"] = POST_TOOL_SUCCESS
    return body, facts


def _mcp_response(response):
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except (ValueError, RecursionError):
            return response
        if isinstance(decoded, (dict, list)):
            response = decoded
    if isinstance(response, dict):
        # MCP content blocks form a list. A skill_load result uses content for its body text.
        if (isinstance(response.get("content"), list)
                or any(key in response for key in ("structuredContent", "isError", "error"))):
            return response
        return {"structuredContent": response}
    return {"content": response} if isinstance(response, list) else response


def has_test_result(status: str, source: str | None) -> bool:
    return (status, source) in {("completed", POST_TOOL_SUCCESS), ("failed", SHELL_FAILURE_EXIT)}


def _shell_status(arguments: dict, response) -> str:
    if not isinstance(response, dict):
        return "unknown"
    if response.get("interrupted") is True:
        return "interrupted"
    if response.get("isError") is True or response.get("error"):
        return "failed"
    # Returning a task handle only confirms launch. The Stop hook's task list
    # describes outstanding tasks across the session, not a command exit.
    if arguments.get("run_in_background") is True or response.get("backgroundTaskId"):
        return "unknown"
    if response.get("interrupted") is False and isinstance(response.get("stdout"), str) and isinstance(response.get("stderr"), str):
        return "completed"
    return "unknown"
