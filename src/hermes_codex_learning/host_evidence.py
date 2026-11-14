"""Bounded Codex 0.167 hook-transcript supplement, never a history scanner."""
from __future__ import annotations

import json
import hashlib
import os
import re
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime

from .normalize import settle_turn
from .store import digest

LIMIT = 16 * 1024 * 1024
HEADER_LIMIT = 2 * 1024 * 1024
CURSOR_VERSION = 2
# The only code-mode form joined without an inner tool ID is a literal call
# whose return is emitted directly. Never evaluate or search arbitrary JS.
_DIRECT_EXEC = re.compile(
    r'text\(\s*await\s+tools\.exec_command\(\s*\{\s*(?:cmd|"cmd")\s*:\s*'
    r'("(?:[^"\\\x00-\x1f]|\\.)*")'
    r'((?:\s*,\s*(?:yield_time_ms|max_output_tokens|"yield_time_ms"|"max_output_tokens")\s*:\s*[0-9]{1,9}){0,2})'
    r'\s*\}\s*\)\s*\)\s*;?\s*'
)
_EXEC_COMPLETE = re.compile(r'Script completed\nWall time [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n')


def _verify_path(path, sessions_root):
    root = (sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions").resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Host transcript is outside the configured Codex sessions directory")
    stat = path.stat()
    if stat.st_uid != os.getuid():
        raise ValueError("Host transcript ownership is not verified")
    return stat


def _header(stream, chat, project):
    line = stream.readline(HEADER_LIMIT + 1)
    if len(line.encode()) > HEADER_LIMIT or not line.endswith("\n"):
        raise ValueError("Host transcript metadata exceeds the bounded header")
    first = json.loads(line)
    meta = first.get("payload", {})
    if (first.get("type") != "session_meta" or meta.get("id") != chat
            or not str(meta.get("cli_version", "")).startswith("0.167.")
            or not Path(meta.get("cwd") or "/").resolve().is_relative_to(Path(project).resolve())):
        raise ValueError("Host transcript identity or schema is not verified")
    return meta


def _relation(meta):
    parent = meta.get("forked_from_id")
    if not isinstance(parent, str) or not re_safe_id(parent) or parent == meta.get("id"):
        return {"status":"unavailable", "reason":"host_parent_fields_missing", "selected_text":"not_supplied"}
    ordinal = meta.get("forked_from_ordinal_exclusive")
    base = meta.get("history_base") or {}
    if not isinstance(base, dict) or (base.get("thread_id") and base["thread_id"] != parent):
        return {"status":"unavailable", "reason":"host_parent_fields_conflict"}
    if base.get("end_ordinal_exclusive") is not None and base["end_ordinal_exclusive"] != ordinal:
        return {"status":"unavailable", "reason":"host_fork_fields_conflict"}
    result = {"status":"verified", "kind":"sidechat" if meta.get("attached_thread") is True else "fork",
              "parent_chat":parent, "basis":"host_session_meta", "selected_text":"not_supplied"}
    if type(ordinal) is not int or ordinal < 0:
        return {**result, "status":"partial", "reason":"host_fork_position_missing"}
    result["fork_ordinal_exclusive"] = ordinal
    offset = base.get("end_byte_offset")
    if type(offset) is int and offset >= 0:
        result["fork_byte_offset"] = offset
    timestamp = meta.get("timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            result["fork_at"] = parsed.timestamp()
    except (ValueError, AttributeError, TypeError):
        pass
    return result


def re_safe_id(value):
    return 0 < len(value) <= 200 and all(char.isascii() and (char.isalnum() or char in "-_") for char in value)


def _boundary_hash(path, offset):
    with path.open("rb") as stream:
        stream.seek(max(0,offset-128))
        return hashlib.sha256(stream.read(min(128,offset))).hexdigest()


def read_host_relation(path: Path, chat: str, project: str, *, sessions_root: Path | None = None) -> dict:
    """Read only the host's relation fields; directory proximity proves no relation."""
    _verify_path(path, sessions_root)
    with path.open() as stream:
        return _relation(_header(stream, chat, project))


def _code_session(row, payload):
    """Read the host's structured shell yield, retaining no shell output."""
    metadata = row.get("metadata") or {}
    blocks = payload.get("output")
    if (not isinstance(metadata, dict) or metadata.get("client_authored") is not False
            or not isinstance(blocks, list) or len(blocks) != 2
            or any(not isinstance(block, dict) or block.get("type") != "input_text"
                   or not isinstance(block.get("text"), str) for block in blocks)
            or not _EXEC_COMPLETE.fullmatch(blocks[0]["text"]) or len(blocks[1]["text"]) > 65536):
        return None
    try:
        body = json.loads(blocks[1]["text"])
    except ValueError:
        return None
    if (isinstance(body, dict) and set(body) == {"chunk_id", "wall_time_seconds", "session_id", "original_token_count", "output"}
            and isinstance(body["chunk_id"], str) and re_safe_id(body["chunk_id"])
            and type(body["wall_time_seconds"]) in {int, float} and 0 <= body["wall_time_seconds"] < LIMIT
            and type(body["session_id"]) is int and 0 < body["session_id"] < 2**63
            and type(body["original_token_count"]) is int and body["original_token_count"] >= 0
            and isinstance(body["output"], str)):
        return body["session_id"]
    return None


def _record_code_exec(calls, row, payload, position):
    call_id, kind = payload.get("call_id"), payload.get("type")
    if kind not in {"custom_tool_call", "custom_tool_call_output"} or not isinstance(call_id, str) or not re_safe_id(call_id):
        return
    if kind == "custom_tool_call":
        if call_id in calls:
            calls[call_id].update(conflict=True)
            return
        calls[call_id] = {}
        script = payload.get("input")
        if (payload.get("name") != "exec" or payload.get("status") != "completed"
                or not isinstance(script, str) or len(script) > 65536):
            return
        match = _DIRECT_EXEC.fullmatch(script)
        if not match:
            return
        options = re.findall(r'yield_time_ms|max_output_tokens', match[2])
        if len(options) != len(set(options)):
            return
        try:
            command = json.loads(match[1])
        except ValueError:
            return
        calls[call_id] = {"command_hash": digest(command), "invoked": position}
    else:
        call = calls.setdefault(call_id, {})
        if "returned" in call:
            call["conflict"] = True
        elif "command_hash" in call:
            call.update(returned=position, session=_code_session(row, payload))


def read_turn(path: Path, chat: str, turn: str, project: str, *, sessions_root: Path | None = None,
              include_task: bool = True, cursor: dict | None = None) -> dict:
    stat = _verify_path(path, sessions_root)
    if include_task and stat.st_size > LIMIT:
        raise ValueError("Host transcript ownership or bounded-read limit failed")
    inputs, commands, trace, code_execs = [], {}, {}, {}
    with path.open() as stream:
        meta = _header(stream, chat, project)
        identity = [stat.st_dev, stat.st_ino, chat, turn]
        if (cursor and cursor.get("version") == CURSOR_VERSION and cursor.get("identity") == identity
                and cursor.get("include_task", False) == include_task
                and stream.tell() <= cursor.get("offset", -1) <= stat.st_size
                and cursor.get("boundary_hash") == _boundary_hash(path,cursor["offset"])):
            stream.seek(cursor["offset"])
            commands = dict(cursor.get("commands", {}))
            trace = dict(cursor.get("trace", {}))
            code_execs = dict(cursor.get("code_execs", {}))
            inputs = list(cursor.get("inputs", [])) if include_task else []
        elif not include_task and stat.st_size > LIMIT:
            stream.seek(max(stream.tell(), stat.st_size - LIMIT))
            stream.readline()  # Discard the partial old record at the bounded tail.
            trace["prefix_omitted"] = True
        offset = stream.tell()
        for line in iter(stream.readline, ""):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    break  # The host may still be appending the final record.
                raise
            offset = stream.tell()
            payload = row.get("payload", {})
            msg_meta = payload.get("internal_chat_message_metadata_passthrough") or {}
            matches = payload.get("turn_id") == turn or msg_meta.get("turn_id") == turn
            ordinal, stamp = row.get("ordinal"), row.get("timestamp")
            position = {"ordinal":ordinal, "timestamp":stamp} if type(ordinal) is int else {"timestamp":stamp}
            if (row.get("type") == "response_item" and msg_meta.get("turn_id") == turn
                    and payload.get("turn_id", turn) == turn and not trace.get("prefix_omitted")):
                _record_code_exec(code_execs, row, payload, position)
            if matches and (row.get("type") == "turn_context" or payload.get("type") == "task_started"):
                trace.setdefault("start", position)
            if matches and row.get("type") == "response_item" and payload.get("role") in {"user","assistant"}:
                role = payload["role"]
                trace.setdefault("request" if role == "user" else "result", position)
                if role == "assistant":
                    trace["result"] = position
                if payload.get("id"):
                    trace.setdefault("messages", {})[payload["id"]] = {**position, "role":role}
            if (row.get("type") != "event_msg" or payload.get("type") != "item_completed"
                    or payload.get("turn_id") != turn):
                continue
            item = payload.get("item", {})
            if item.get("type") in {"CommandExecution","McpToolCall","FileChange"} and item.get("id"):
                commands[item["id"]] = {key: item.get(key) for key in ("status", "exit_code", "process_id")}
                commands[item["id"]]["kind"] = item["type"]
                commands[item["id"]].update(position)
            if not include_task:
                continue
            if (item.get("type") != "FunctionCallOutput" or item.get("namespace") != "codex_app"
                    or item.get("name") not in {"create_thread", "send_message_to_thread"} or not item.get("id")):
                continue
            output = item.get("output")
            if isinstance(output, dict):
                output = None if output.get("truncated") else output.get("text")
            if not isinstance(output, str) or len(output) > 65536 or "<!" in output:
                continue
            try:
                xml = ET.fromstring(output)
            except ET.ParseError:
                continue
            if xml.tag == "codex_delegation" and xml.findtext("input") and xml.findtext("source_thread_id"):
                inputs.append({"prompt": xml.findtext("input"), "source_thread": xml.findtext("source_thread_id"),
                               "item_id": item["id"], "item_hash": digest(item), "method": item["name"]})
    if len(inputs) > 1:
        raise ValueError("Ambiguous task specifications in one host turn")
    result = {"input": inputs[0] if inputs else None, "commands": commands, "relation":_relation(meta), "trace":trace,
              "code_execs":code_execs,"cwd_hash":digest(meta.get("cwd"))}
    result["cursor"] = {"version":CURSOR_VERSION,"identity":identity,"offset":offset,"commands":commands,"trace":trace,"code_execs":code_execs,
                        "include_task":include_task, "inputs":inputs if include_task else [],"boundary_hash":_boundary_hash(path,offset)}
    return result


def _at(position):
    try:
        stamp = datetime.fromisoformat(position["timestamp"].replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _join_background(store, data, facts, transcript_path):
    calls = facts.get("code_execs", {})
    if data.get("_ended_by") != "Stop" or not calls:
        return
    source = data["source"]
    # Only recover previously unobserved input here. An existing native
    # receipt for this event cannot be replaced with a different body.
    turn = store.db.execute("SELECT observed_revision FROM turns WHERE id=?", (digest([source["chat"], source["turn"]]),)).fetchone()
    if turn and turn["observed_revision"] is not None:
        return
    terminal = data.get("evidence_provenance", {}).get("terminal_event")
    def same_scope(event):
        return (event.get("bridge_host"), event.get("bridge_project"), event.get("session_id"),
                event.get("turn_id"), event.get("transcript_path")) == (
                    source["host"], source["project"], source["chat"], source["turn"], transcript_path)

    stop = store.db.execute("SELECT created,payload FROM events WHERE id=?", (terminal,)).fetchone()
    if not stop:
        return
    stop_event = json.loads(stop["payload"])
    if stop_event.get("hook_event_name") != "Stop" or not same_scope(stop_event):
        return
    starts = {}
    for row in store.db.execute("SELECT id,created,payload FROM events WHERE json_extract(payload,'$.session_id')=? "
                                "AND json_extract(payload,'$.turn_id')=? AND json_extract(payload,'$.hook_event_name')='PreToolUse'",
                                (source["chat"], source["turn"])):
        event = json.loads(row["payload"])
        arguments = event.get("tool_input")
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if (same_scope(event) and digest(event.get("cwd")) == facts["cwd_hash"] and event.get("tool_name") == "Bash"
                and isinstance(command, str) and event.get("tool_use_id")):
            starts.setdefault(digest(command), []).append((row, event["tool_use_id"], command))
    commands = Counter(call.get("command_hash") for call in calls.values())
    sessions = Counter(call.get("session") for call in calls.values())
    for outer_id, call in calls.items():
        command_hash, session = call.get("command_hash"), call.get("session")
        matching = starts.get(command_hash, [])
        if not session or call.get("conflict") or len(matching) != 1 or commands[command_hash] != 1 or sessions[session] != 1:
            continue
        pre, tool_id, command = matching[0]
        tool = data["tools"].get(tool_id)
        if (not tool or tool.get("status") != "running" or tool.get("result") is not None or tool.get("process")
                or tool.get("name") != "Bash" or tool.get("arguments", {}).get("command") != command):
            continue
        invoked, returned = _at(call["invoked"]), _at(call["returned"])
        if (invoked is not None and returned is not None and invoked <= pre["created"] <= returned <= stop["created"]
                and not any(other.get("process") == str(session) for other in data["tools"].values())):
            tool.update(process=str(session), background_session={"outer_call_id":outer_id, "pre_event":pre["id"],
                                                                 "observed_at":call["returned"]["timestamp"],
                                                                 "session_id":str(session),
                                                                 "transcript_ref":digest(transcript_path)})


def supplement(store, data: dict, *, settings=None, include_task=True, event_id=None) -> dict:
    source = data["source"]
    row = store.db.execute("SELECT id,payload FROM events WHERE json_extract(payload,'$.session_id')=? "
                           "AND json_extract(payload,'$.turn_id')=? AND json_extract(payload,'$.transcript_path') IS NOT NULL "
                           "ORDER BY created DESC LIMIT 1", (source["chat"], source["turn"])).fetchone()
    if not row:
        return data
    event = json.loads(row["payload"])
    if event.get("bridge_project") != source["project"] or event.get("bridge_host") != source["host"]:
        return data
    try:
        # A retained hook prompt needs only terminal facts, never another full body scan.
        read_task = include_task and not data.get("user")
        cursor_key = "transcript-cursor:" + digest([source["chat"], source["turn"], read_task])
        facts = read_turn(Path(event["transcript_path"]), source["chat"], source["turn"], source["project"],
                          include_task=read_task, cursor=store.get(cursor_key))
        if not read_task and facts.get("cursor"):
            # A task body has not yet passed observation admission. It cannot
            # become a durable cursor payload on the way to that check.
            store.set(cursor_key, facts["cursor"])
    except (OSError, ValueError, TypeError):
        return {**data, "coverage": sorted(set(data.get("coverage", [])) | {"host_transcript_input_unavailable"})}
    result = {**data, "tools": {key: dict(value) for key, value in data["tools"].items()}}
    result["host_context"] = {**result.get("host_context", {}), "relation":facts.get("relation",{})}
    result["host_trace"] = facts.get("trace",{})
    task = facts["input"]
    if not result.get("user") and task:
        result["user"] = "Codex App task specification from a verified host transcript (not a direct user declaration).\n\n" + task["prompt"]
        result["task_input_provenance"] = {"kind": "native_hook_transcript", "hook_event": row["id"],
                                           **{key: task[key] for key in ("source_thread", "item_id", "item_hash", "method")}}
    resolved = set()
    for identifier, fact in facts["commands"].items():
        if identifier not in result["tools"]:
            continue
        tool = result["tools"][identifier]
        background = tool.get("background_session")
        if background:
            exit_at = _at(fact)
            launched_at = _at({"timestamp":background["observed_at"]})
            if (background.get("exit_event") or event_id is None or background.get("pending_exit_event") != event_id
                    or background["transcript_ref"] != digest(event["transcript_path"])
                    or exit_at is None or launched_at is None or exit_at < launched_at):
                continue
            if fact.get("kind") != "CommandExecution" or not (fact["status"] == "interrupted" or
                    (str(fact["status"]).lower() in {"completed", "failed"} and type(fact["exit_code"]) is int)):
                continue
        result["tools"][identifier]["host_position"] = {key:fact[key] for key in ("ordinal","timestamp") if key in fact}
        status = str(fact["status"]).lower().replace("_", "")
        code = fact["exit_code"]
        if status == "interrupted":
            result["tools"][identifier].update(status="interrupted", process="", status_source="host_transcript")
        elif fact.get("kind") == "CommandExecution" and type(code) is int and status in {"completed", "failed"}:
            original = result["tools"][identifier].get("result")
            if isinstance(original, dict) and original.get("status_source") == "host_transcript":
                original = original.get("output")
            result["tools"][identifier].update(status="completed" if code == 0 else "failed", process="",
                status_source="host_transcript",
                result={"output": original, "exit_code": code, "status_source": "host_transcript"} if include_task
                       else {'outcome': 'completed' if code == 0 else 'failed'})
        elif status in {"inprogress", "running"}:
            result["tools"][identifier].update(status="running", process=str(fact["process_id"] or ""))
        elif status == "failed":
            result["tools"][identifier].update(status=status,process="",status_source="host_transcript")
            if not include_task or result["tools"][identifier].get("result") is None:
                result["tools"][identifier]["result"]={"outcome":status}
        elif fact.get("kind") in {"McpToolCall","FileChange"} and status == "completed":
            result["tools"][identifier].update(status="completed",process="",status_source="host_transcript")
            if not include_task or result["tools"][identifier].get("result") is None:
                result["tools"][identifier]["result"]={"outcome":"completed"}
        if result["tools"][identifier]["status"] in {"completed", "failed", "interrupted"}:
            resolved.add("unrecognized_execution_result:" + identifier)
            if background:
                tool["background_session"] = {k: v for k, v in background.items() if k != "pending_exit_event"}
                tool["background_session"]["exit_event"] = background["pending_exit_event"]
    if resolved and "coverage" in result:
        result["coverage"] = sorted(set(result["coverage"]) - resolved)
    if include_task:
        _join_background(store, result, facts, event["transcript_path"])
    if result.get("_ended_by") in {"Stop", "Interrupt"}:
        settle_turn(result)
    return result
