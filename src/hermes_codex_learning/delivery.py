"""Technical delivery outbox, independent of task-body admission."""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from .store import digest, dumps

_SHELL_READERS = {"cat", "sed", "head", "tail", "bat", "less", "more", "awk", "python", "python3"}
_READ_TOOLS = {"read", "read_file", "view"}
_EXEC_TOOLS = {"bash", "exec_command", "terminal", "exec"}


def _tool_succeeded(payload: dict, tool_kind: str) -> bool:
    raw = payload.get("tool_response")
    if raw is None:
        return False
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return "Process exited with code 0" in raw or tool_kind in _READ_TOOLS
        raw = parsed
    if not isinstance(raw, dict) or raw.get("isError") is True or raw.get("interrupted") is True or raw.get("error"):
        return False
    if "structuredContent" in raw and isinstance(raw["structuredContent"], dict):
        inner = raw["structuredContent"]
        if inner.get("isError") is True or inner.get("interrupted") is True or inner.get("error"):
            return False
        if tool_kind in _EXEC_TOOLS and "exit_code" in inner:
            return inner.get("exit_code") == 0
    if tool_kind in _EXEC_TOOLS and "exit_code" in raw:
        return raw.get("exit_code") == 0
    return True


def _candidate_read_paths(payload: dict, tool_kind: str) -> list[str]:
    args = payload.get("tool_input")
    if not isinstance(args, dict):
        return []
    if tool_kind in _READ_TOOLS:
        value = args.get("file_path") or args.get("path")
        return [value] if isinstance(value, str) and value else []
    if tool_kind not in _EXEC_TOOLS:
        return []
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str) or not command or len(command) > 65536:
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if not tokens:
        return []
    has_reader = any(tok.rsplit("/", 1)[-1] in _SHELL_READERS for tok in tokens if not tok.startswith("-"))
    if not has_reader:
        return []
    return [tok for tok in tokens if "/" in tok or tok.endswith(".md") or tok.endswith(".py") or tok.endswith(".sh")]


def _resolve_projected_skill_file(cand: str, base_cwd: str | None, settings, projection: dict,
                                  *, output: bytes | None = None, allow_partial: bool = False) -> dict | None:
    from .native_projection import catalog_root, snapshots_root
    versions = projection.get("versions") if isinstance(projection, dict) else None
    if not isinstance(versions, dict) or not versions:
        return None
    raw_path = Path(cand).expanduser()
    if not raw_path.is_absolute():
        if not base_cwd or not Path(base_cwd).is_absolute():
            return None
        raw_path = Path(base_cwd) / raw_path

    snaps_root = snapshots_root(settings)
    if not snaps_root.is_dir():
        return None
    snaps_resolved = snaps_root.resolve()

    matched_name = None
    for candidate_root in filter(None, (settings.codex_skills_dir, catalog_root(settings))):
        try:
            rel_parts = raw_path.absolute().relative_to(candidate_root.absolute()).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] in versions:
            matched_name = rel_parts[0]
            break

    try:
        resolved_file = raw_path.resolve(strict=True)
    except OSError:
        return None
    if not resolved_file.is_file() or not resolved_file.is_relative_to(snaps_resolved):
        return None
    snap_parts = resolved_file.relative_to(snaps_resolved).parts
    if len(snap_parts) < 2:
        return None
    version = snap_parts[0]
    rel_file = str(Path(*snap_parts[1:]))
    if matched_name is None:
        matched_name = next((name for name, ver in versions.items() if ver == version), None)
    if not matched_name or versions.get(matched_name) != version:
        return None
    try:
        body = resolved_file.read_bytes()
    except OSError:
        return None
    artifact = {"name": matched_name, "version": version, "file_path": rel_file,
                "content_hash": hashlib.sha256(body).hexdigest(), "scope": "global"}
    if output is not None:
        if output == body:
            artifact["read_coverage"] = "full"
        elif allow_partial and output in body:
            artifact["read_coverage"] = "partial"
        else:
            return None
    return artifact


def _capture_native_skill_read(store, payload: dict, project: str, settings, *,
                               match_output: bool = False, allow_partial: bool = False) -> None:
    if settings is None or payload.get("hook_event_name") != "PostToolUse":
        return
    if not all(isinstance(payload.get(key), str) and payload[key]
               for key in ("session_id", "turn_id", "tool_use_id")):
        return
    tool_kind = str(payload.get("tool_name", "")).rsplit(".", 1)[-1].lower()
    if tool_kind not in (_READ_TOOLS | _EXEC_TOOLS):
        return
    succeeded = _tool_succeeded(payload, tool_kind)
    if match_output:
        # This worker path supplements shell responses the installed hook cannot verify.
        raw = payload.get("tool_response")
        if succeeded or tool_kind not in _EXEC_TOOLS or not isinstance(raw, str) or not raw.strip():
            return
    elif not succeeded:
        return
    projection = store.get("native-skill-projection")
    if not projection:
        return
    candidates = _candidate_read_paths(payload, tool_kind)
    if not candidates:
        return
    if allow_partial:
        from .workflow import action
        allow_partial = action(payload) == "inspect"
    try:
        output = raw.encode("utf-8") if match_output else None
    except UnicodeEncodeError:
        return
    from .read_receipts import initialize, issue
    initialize(settings.state)
    seen = set()
    for cand in candidates[:12]:
        artifact = _resolve_projected_skill_file(cand, payload.get("cwd") or project, settings, projection,
                                                output=output, allow_partial=allow_partial)
        if not artifact:
            continue
        dedup = (artifact["name"], artifact["version"], artifact["file_path"])
        if dedup in seen:
            continue
        seen.add(dedup)
        token = issue(settings.state, artifact, project)
        safe = {
            "delivery_token": token,
            "name": artifact["name"],
            "version": artifact["version"],
            "file_path": artifact["file_path"],
            "delivery_mode": "native_projection",
        }
        if "read_coverage" in artifact:
            safe["read_coverage"] = artifact["read_coverage"]
        event = {
            "session_id": payload["session_id"],
            "turn_id": payload["turn_id"],
            "tool_use_id": payload["tool_use_id"],
            "tool_name": "native_skill_file_read",
            "bridge_project": project,
            "tool_response": {"structuredContent": safe},
        }
        with store.db:
            identifier = digest(["native_projection", project, event["session_id"], event["turn_id"],
                                 event["tool_use_id"], dedup])
            store.db.execute(
                "INSERT OR IGNORE INTO delivery_events VALUES (?,?,NULL)",
                (identifier, dumps(event)),
            )


def capture_observed_skill_reads(store, turn, settings):
    """Recover delivery from a captured shell result and, for excerpts, its host exit."""
    source = turn["source"]
    for tool in turn["tools"].values():
        if tool["status"] not in {"completed", "unknown"}:
            continue
        response = tool.get("result")
        allow_partial = False
        if tool.get("status_source") == "host_transcript" and isinstance(response, dict):
            allow_partial = type(response.get("exit_code")) is int and response["exit_code"] == 0
            response = response.get("output")
        if not isinstance(response, str):
            continue
        arguments = tool.get("arguments") or {}
        cwd = arguments.get("workdir") or arguments.get("cwd")
        event = {"hook_event_name": "PostToolUse", "session_id": source["chat"], "turn_id": source["turn"],
                 "tool_use_id": tool["call_id"], "tool_name": tool["name"], "tool_response": response,
                 "tool_input": arguments, "cwd": cwd if isinstance(cwd, str) else source["project"]}
        _capture_native_skill_read(store, event, source["project"], settings, match_output=True,
                                   allow_partial=allow_partial)


def capture_delivery(store, payload: dict, project: str, *, settings=None) -> None:
    if (payload.get("hook_event_name") != "PostToolUse"
            or "hermes_learning" not in payload.get("tool_name", "")
            or not payload.get("tool_name", "").endswith("skill_load")
            or not all(isinstance(payload.get(key), str) and payload[key]
                       for key in ("session_id", "turn_id", "tool_use_id"))):
        _capture_native_skill_read(store, payload, project, settings)
        return
    result = payload.get("tool_response", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return
    if not isinstance(result, dict) or result.get("isError"):
        return
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    value = json.loads(block["text"])
                except (ValueError, KeyError):
                    continue
                if isinstance(value, dict) and value.get("delivery_token"):
                    break
    if not isinstance(value, dict):
        return
    token = value.get("delivery_token")
    if not isinstance(token, str) or not token or len(token) > 4096:
        return
    # Native issuance validates the opaque receipt. Never copy skill text/resources.
    safe = {key: value[key] for key in ("delivery_token", "name", "version")
            if isinstance(value.get(key), str) and len(value[key]) <= (4096 if key=="delivery_token" else 256)}
    event = {key: payload[key] for key in ("session_id", "turn_id", "tool_use_id", "tool_name")}
    event.update(bridge_project=project, tool_response={"structuredContent": safe})
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO delivery_events VALUES (?,?,NULL)",
                         (digest([token, project, event["session_id"], event["turn_id"], event["tool_use_id"]]), dumps(event)))


def drain_deliveries(store, usage) -> None:
    import time
    for row in store.db.execute("SELECT id,payload FROM delivery_events WHERE delivered IS NULL LIMIT 100").fetchall():
        try:
            usage(json.loads(row["payload"]))
        except (ValueError, PermissionError):
            # A forged/expired receipt is terminal, not a poison item for the outbox.
            store.set("delivery-rejection:" + row["id"], {"reason":"invalid_or_expired_receipt"})
        with store.db:
            store.db.execute("UPDATE delivery_events SET delivered=? WHERE id=?", (time.time(), row["id"]))
