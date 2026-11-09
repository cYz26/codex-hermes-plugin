"""Bounded Codex 0.167 hook-transcript supplement, never a history scanner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from .store import digest

LIMIT = 16 * 1024 * 1024


def read_turn(path: Path, chat: str, turn: str, project: str, *, sessions_root: Path | None = None,
              include_task: bool = True) -> dict:
    root = (sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions").resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Host transcript is outside the configured Codex sessions directory")
    if path.stat().st_uid != os.getuid() or path.stat().st_size > LIMIT:
        raise ValueError("Host transcript ownership or bounded-read limit failed")
    inputs, commands = [], {}
    with path.open() as stream:
        first = json.loads(stream.readline())
        meta = first.get("payload", {})
        if (first.get("type") != "session_meta" or meta.get("id") != chat
                or not str(meta.get("cli_version", "")).startswith("0.167.")
                or not Path(meta.get("cwd") or "/").resolve().is_relative_to(Path(project).resolve())):
            raise ValueError("Host transcript identity or schema is not verified")
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    break  # The host may still be appending the final record.
                raise
            payload = row.get("payload", {})
            if (row.get("type") != "event_msg" or payload.get("type") != "item_completed"
                    or payload.get("turn_id") != turn):
                continue
            item = payload.get("item", {})
            if item.get("type") == "CommandExecution" and item.get("id"):
                commands[item["id"]] = {key: item.get(key) for key in ("status", "exit_code", "process_id")}
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
    return {"input": inputs[0] if inputs else None, "commands": commands}


def supplement(store, data: dict, *, settings=None, include_task=True) -> dict:
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
        facts = read_turn(Path(event["transcript_path"]), source["chat"], source["turn"], source["project"],
                          include_task=include_task)
    except (OSError, ValueError, TypeError):
        return {**data, "coverage": sorted(set(data.get("coverage", [])) | {"host_transcript_input_unavailable"})}
    result = {**data, "tools": {key: dict(value) for key, value in data["tools"].items()}}
    task = facts["input"]
    if not result.get("user") and task:
        result["user"] = "Codex App task specification from a verified host transcript (not a direct user declaration).\n\n" + task["prompt"]
        result["task_input_provenance"] = {"kind": "native_hook_transcript", "hook_event": row["id"],
                                           **{key: task[key] for key in ("source_thread", "item_id", "item_hash", "method")}}
    for identifier, fact in facts["commands"].items():
        if identifier not in result["tools"]:
            continue
        status = str(fact["status"]).lower().replace("_", "")
        code = fact["exit_code"]
        if type(code) is int:
            original = result["tools"][identifier].get("result")
            if isinstance(original, dict) and original.get("status_source") == "host_transcript":
                original = original["output"]
            result["tools"][identifier].update(status="completed" if code == 0 else "failed", process="",
                result={"output": original, "exit_code": code, "status_source": "host_transcript"} if include_task
                       else {'outcome': 'completed' if code == 0 else 'failed'})
        elif status in {"inprogress", "running"}:
            result["tools"][identifier].update(status="running", process=str(fact["process_id"] or ""))
        elif status in {"failed", "interrupted"}:
            result["tools"][identifier]["status"] = status
    if result.get("_ended_by") == "Stop":
        result["status"] = "incomplete" if any(t["status"] in {"running", "unknown"} for t in result["tools"].values()) else "completed"
    return result
