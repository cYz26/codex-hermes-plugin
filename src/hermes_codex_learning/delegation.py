"""Join public App-tool hook receipts; keep task specifications distinct from user declarations."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .store import Store

SUGGESTION_PREFIX = (
    "This is an app-generated request for a suggested next user message. "
    "The user did not write this message."
)


def exclude_internal(store: Store, payload: dict) -> bool:
    chat = payload.get("session_id")
    if not chat:
        return False
    key = "excluded-chat:" + chat
    if store.get(key):
        return True
    prompt = payload.get("prompt")
    if (payload.get("hook_event_name") == "UserPromptSubmit"
            and not payload.get("transcript_path") and isinstance(prompt, str)
            and prompt.startswith(SUGGESTION_PREFIX)):
        store.set(key, {"reason": "app_generated_next_message_suggestion", "source": "native_hook_marker"})
        return True
    return False


def _result_object(response):
    if not isinstance(response, dict) or response.get("isError"):
        return None
    structured = response.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in response.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                value = json.loads(item.get("text", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
    return None


def register_delegation(store: Store, event_id: str, payload: dict, *, settings=None) -> bool:
    if payload.get("hook_event_name") == "PostToolUse" and payload.get("tool_name") == "mcp__codex_app__read_thread":
        return _register_readback(store, event_id, payload, settings=settings)
    if (payload.get("hook_event_name") != "PostToolUse"
            or payload.get("tool_name") != "mcp__codex_app__create_thread"
            or not all(payload.get(key) for key in ("session_id", "turn_id", "tool_use_id", "bridge_host", "bridge_project"))):
        return False
    arguments = payload.get("tool_input")
    result = _result_object(payload.get("tool_response"))
    if not isinstance(arguments, dict) or not isinstance(result, dict):
        return False
    prompt, chat = arguments.get("prompt"), result.get("threadId")
    if (not isinstance(prompt, str) or not prompt.strip() or not isinstance(chat, str) or not chat
            or chat == payload["session_id"] or result.get("hostId") != "local"):
        return False
    value = {"prompt": prompt, "child_chat": chat, "host": payload["bridge_host"],
             "project": payload["bridge_project"], "source_event": event_id,
             "parent_chat": payload["session_id"], "parent_turn": payload["turn_id"],
             "parent_tool_call": payload["tool_use_id"]}
    if settings is not None and settings.capture_scope == "global":
        value["cross_project_allowed"] = True
    previous = store.get("delegated-input:" + chat)
    if previous:
        if any(previous.get(key) != value[key] for key in value if key not in {"source_event", "cross_project_allowed"}):
            raise ValueError("Conflicting App delegation receipts for one child chat")
        return True
    store.set("delegated-input:" + chat, value)
    return True


def _sender_receipt(store: Store, payload: dict, chat: str, source: str, prompt: str, method: str):
    matches = []
    for row in store.db.execute("SELECT id,payload FROM events WHERE "
                                "json_extract(payload,'$.hook_event_name')='PostToolUse' "
                                "AND json_extract(payload,'$.session_id')=? "
                                "AND json_extract(payload,'$.tool_name')=?", (source, "mcp__codex_app__" + method)):
        parent = json.loads(row["payload"])
        arguments, result = parent.get("tool_input", {}), _result_object(parent.get("tool_response"))
        if (not isinstance(result, dict) or result.get("threadId") != chat or arguments.get("prompt") != prompt
                or any(parent.get(key) != payload.get(key) for key in ("bridge_host", "bridge_project"))
                or not parent.get("turn_id") or not parent.get("tool_use_id")
                or (method == "send_message_to_thread" and arguments.get("threadId") != chat)):
            continue
        matches.append((row["id"], parent))
    return matches[0] if len(matches) == 1 else None


def _register_readback(store: Store, event_id: str, payload: dict, *, settings=None) -> bool:
    result = _result_object(payload.get("tool_response"))
    if not isinstance(result, dict) or result.get("schemaVersion") != 1:
        return False
    thread = result.get("thread", {})
    chat = thread.get("id")
    project = payload.get("bridge_project")
    if settings is not None and settings.capture_scope == "global":
        project = settings.project_for(thread.get("cwd"))
    if (not chat or not project or thread.get("hostId") != "local"
            or payload.get("tool_input", {}).get("threadId") != chat
            or not Path(thread.get("cwd") or "/").resolve().is_relative_to(Path(project).resolve())):
        return False
    recorded = False
    for turn in result.get("turns", []):
        if not isinstance(turn.get("id"), str):
            continue
        for item in turn.get("items", []):
            method, output = item.get("name"), item.get("output", {})
            if (item.get("type") != "functionCallOutput" or item.get("namespace") != "codex_app"
                    or method not in {"create_thread", "send_message_to_thread"}
                    or not isinstance(output, dict) or output.get("truncated")
                    or not isinstance(output.get("text"), str)):
                continue
            try:
                xml = ET.fromstring(output["text"])
            except ET.ParseError:
                continue
            if xml.tag != "codex_delegation":
                continue
            source, prompt = xml.findtext("source_thread_id"), xml.findtext("input")
            sender = _sender_receipt(store, payload, chat, source, prompt, method)
            if not sender:
                continue
            source_event, parent = sender
            value = {"prompt": prompt, "child_chat": chat, "child_turn": turn["id"],
                     "host": parent["bridge_host"], "project": project, "source_event": source_event,
                     "parent_chat": parent["session_id"], "parent_turn": parent["turn_id"],
                     "parent_tool_call": parent["tool_use_id"], "read_event": event_id, "tool_method": method}
            key = "delegated-turn-input:" + chat + ":" + turn["id"]
            previous = store.get(key)
            if previous and any(previous[k] != value[k] for k in ("source_event", "prompt", "child_turn")):
                raise ValueError("Conflicting App task readbacks for one child turn")
            if not previous:
                store.set(key, value)
            recorded = True
    return recorded


def task_input(store: Store, data: dict, *, settings=None) -> dict:
    if data.get("user"):
        return data
    source = data["source"]
    value = (store.get("delegated-turn-input:" + source["chat"] + ":" + source["turn"])
             or store.get("delegated-input:" + source["chat"]))
    if not value or value["host"] != source["host"]:
        return data
    if value["project"] != source["project"]:
        # A cross-project create receipt needs an actual destination hook, not a guessed cwd.
        if not (settings is not None and settings.capture_scope == "global" and value.get("cross_project_allowed")):
            return data
        child = store.db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.session_id')=? "
                                 "AND json_extract(payload,'$.bridge_host')=? "
                                 "AND json_extract(payload,'$.bridge_project')=? LIMIT 1",
                                 (source["chat"], source["host"], source["project"])).fetchone()
        if not child:
            return data
    if not value.get("child_turn"):
        first = store.db.execute("SELECT json_extract(payload,'$.turn_id') FROM events "
                                  "WHERE json_extract(payload,'$.session_id')=? "
                                  "AND json_extract(payload,'$.turn_id') IS NOT NULL "
                                  "ORDER BY created,id LIMIT 1", (source["chat"],)).fetchone()
        value = {**value, "child_turn": first[0] if first else source["turn"]}
        store.set("delegated-input:" + source["chat"], value)
    if value["child_turn"] != source["turn"]:
        return data
    provenance = {key: value[key] for key in ("source_event", "parent_chat", "parent_turn", "parent_tool_call", "read_event")
                  if key in value}
    return {**data, "user": "Codex App delegated task specification (not a direct user declaration).\n"
            "Source: parent " + value.get("tool_method", "create_thread") + " hook receipt " + value["source_event"] + "\n\n" + value["prompt"],
            "coverage": sorted(set(data.get("coverage", [])) | {"delegated_task_input:" + value["source_event"]}),
            "task_input_provenance": {"kind": "app_public_readback_hook" if value.get("read_event") else "app_create_thread_hook",
                                      **provenance}}
