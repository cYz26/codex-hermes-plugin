"""Local Hermes client transport. Only the bridge owner executes knowledge writes."""
from __future__ import annotations

import hmac
import json
import re
import time
from contextlib import closing, nullcontext

from .store import Store, digest, dumps, redact


VERSION = 1
SURFACES = {"cli", "tui", "gui", "desktop"}
EVENTS = {"prompt": "UserPromptSubmit", "tool_start": "PreToolUse", "tool_end": "PostToolUse",
          "finished": "Stop", "interrupted": "Interrupt"}
WRITES = {"memory", "skills", "decide", "clear", "journey_edit", "journey_delete", "refine", "restore"}
REQUEST_ID = re.compile(r"[a-zA-Z0-9_:-]{1,192}")
READS = {"view", "skill", "pending", "journey", "history", "status", "receipt", "feedback", "archived"}


class Rejected(PermissionError):
    """A validated client request that will be refused on every retry."""


def _require_enterprise_boundary(settings):
    if settings is None:
        return None
    from .enterprise import MODE, boundary
    if settings.data_policy.get("mode") != MODE:
        return None
    approved = boundary(settings.data_policy)
    if not approved:
        raise PermissionError("Enterprise learning boundary is unavailable or changed")
    return approved


def _id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ValueError("Invalid shared learning identifier")
    return value


def chat_id(settings, session):
    return "hermes:" + digest([str(settings.shared_profile), _id(session)])


def _binding(bridge, params):
    settings = bridge.settings
    client = params.get("client")
    if (settings.shared_profile is None or not isinstance(client, str)
            or not hmac.compare_digest(client, settings.shared_client_id)
            or params.get("version") != VERSION or params.get("surface") not in SURFACES):
        raise PermissionError("Shared learning client is not bound to this service")
    return settings


def _project(settings, cwd):
    if cwd is None:
        return None
    project = settings.project_for(cwd)
    if project is None:
        raise PermissionError("This workspace is outside shared learning")
    return project


def _text(value, limit=12288):
    from .review_input import _preview, _without_media
    if isinstance(value, list):
        value = "\n".join(item["text"] for item in value if isinstance(item, dict)
                          and item.get("type") in {"text", "input_text"} and isinstance(item.get("text"), str))
    return _preview(_without_media(value), limit) if isinstance(value, str) else ""


def _tool_result(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {"output": _text(value, 8192)}
    if not isinstance(value, dict):
        return {"output": _text(value, 8192)}
    clean = {key: value[key] for key in ("exit_code", "session_id", "status", "interrupted", "success")
             if key in value and isinstance(value[key], (str, bool, int, type(None)))}
    for key in ("output", "error", "message"):
        if isinstance(value.get(key), str):
            clean[key] = _text(value[key], 8192)
    return clean


def capture_native(settings, store, project, params):
    from . import policy, workflow
    from .enterprise import explicitly_restricted
    from .delivery import capture_delivery

    kind = params.get("kind")
    if kind not in EVENTS:
        raise ValueError("Unknown native lifecycle event")
    session = chat_id(settings, params.get("session"))
    turn = _id(params.get("turn"))
    host = "hermes:" + digest(str(settings.shared_profile))
    if project is None:
        return {"state": "withheld", "reason": "workspace_required", "event_id": None}
    event = {"hook_event_name": EVENTS[kind], "session_id": session, "turn_id": turn, "cwd": project,
             "bridge_host": host, "bridge_project": project, "bridge_executor": "Hermes"}
    key = digest([session, turn])
    project_key = "shared-turn-project:" + key
    origin = store.get(project_key)
    if kind == "prompt":
        if origin is not None and origin != project:
            raise PermissionError("A captured turn cannot change workspace")
        event["prompt"] = _text(params.get("text"))
    elif origin != project:
        return {"state": "awaiting_prompt", "event_id": None}
    elif kind in {"finished", "interrupted"}:
        event["last_assistant_message"] = _text(params.get("text"))
    else:
        name = params.get("tool")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", name):
            raise ValueError("Invalid native tool name")
        event.update(tool_name=name, tool_use_id=_id(params.get("call")))
        content = params.get("args")
        event["tool_input"] = content if isinstance(content, dict) and len(dumps(content).encode()) <= 8192 else {}
        event["bridge_action"] = workflow.action(event)
        if event["bridge_action"] == "test":
            event["bridge_test_key"] = digest([project, event["tool_input"].get("command")])
        if kind == "tool_end":
            event.update(native_status=params.get("status"), tool_response=_tool_result(params.get("result")))
        if name in {"memory", "skill_view", "skills_list", "skill_manage", "session_search", "shared_learning_feedback"}:
            # Delivery receipts are technical evidence. Recalled text and shared
            # mutations aren't observations from an independent execution.
            result = params.get("result")
            if name == "skill_view" and kind == "tool_end" and isinstance(result, dict):
                safe = {field: result[field] for field in ("name", "version", "delivery_token")
                        if isinstance(result.get(field), str)}
                if safe.get("delivery_token"):
                    from .read_receipts import verify
                    proof = verify(settings.state, safe["delivery_token"], project)
                    if any(safe.get(field) != proof[field] for field in ("name", "version")):
                        raise PermissionError("Delivery does not identify the returned skill")
                    capture_delivery(store, {**event, "tool_name": "hermes_learning_skill_load",
                                             "tool_response": {"structuredContent": safe}}, project)
            return {"state": "technical_only", "event_id": None}
    if kind == "prompt":
        store.set(project_key, project)
    blocked_key = "withheld-turn:" + key
    if store.get(blocked_key) or explicitly_restricted(event):
        store.set(blocked_key, "explicitly_restricted_material")
        return {"state": "withheld", "reason": "explicitly_restricted_material", "event_id": None}
    event = redact(event)
    decision = policy.admit(event, settings.data_policy, project)
    if decision.get("allowed"):
        if decision.get("schema_version") == 2:
            event = {**decision["payload"], "bridge_executor": "Hermes", "bridge_evidence_mode": "semantic-v2"}
    elif settings.data_policy.get("workflow_projection") and decision.get("reason") != "explicitly_restricted_material":
        event = {**workflow.project_event(event), "bridge_executor": "Hermes",
                 "bridge_project": project, "bridge_host": host}
    else:
        store.set(blocked_key, decision["reason"])
        return {"state": "withheld", "reason": decision["reason"], "event_id": None}
    if kind == "prompt" and settings.proactive and decision.get("allowed"):
        from .scheduling import record_dialogue_signals
        record_dialogue_signals(event, store)
    event_id = store.capture(event)
    row = store.db.execute("SELECT rowid,delivered FROM events WHERE id=?", (event_id,)).fetchone()
    return {"state": "consumed" if row and row[1] is not None else "queued",
            "event_id": event_id, "cursor": row[0] if row else None}


def enqueue(store, project, params, *, executor="Hermes", settings=None):
    request_id = _id(params.get("request_id"))
    operation, args = params.get("operation"), params.get("args", {})
    if operation not in WRITES or not isinstance(args, dict) or len(dumps(args).encode()) > 128 * 1024:
        raise ValueError("Invalid shared learning operation")
    from .enterprise import MODE, explicitly_restricted, scrub
    _require_enterprise_boundary(settings)
    if redact(args) != args or explicitly_restricted(args):
        raise PermissionError("Shared write contains restricted data")
    if settings is not None and settings.data_policy.get("mode") == MODE and scrub(args) != args:
        raise PermissionError("Shared write contains restricted data")
    if params.get("base") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(params["base"])):
        raise ValueError("Invalid shared knowledge version")
    body = {"operation": operation, "args": args, "base": params.get("base"),
            "source": {"executor": executor, "surface": params["surface"],
                       "chat": params.get("session"), "turn": params.get("turn")}}
    encoded = dumps(body)
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO shared_writes VALUES (?,?,?,'queued',NULL,?)",
                         (request_id, project, encoded, time.time()))
        row = store.db.execute("SELECT project,payload FROM shared_writes WHERE id=?", (request_id,)).fetchone()
        if tuple(row) != (project, encoded):
            raise ValueError("Shared request ID was already used for a different operation")
    return receipt(store, project, request_id)


def receipt(store, project, request_id):
    row = store.db.execute("SELECT state,result FROM shared_writes WHERE id=? AND project IS ?",
                           (_id(request_id), project)).fetchone()
    return ({"request_id": request_id, "state": row[0], "result": json.loads(row[1]) if row[1] else None}
            if row else {"request_id": request_id, "state": "unknown"})


def status(store, project, *, settings=None):
    from .enterprise import boundary
    active = boundary(settings.data_policy) if settings is not None else None
    counts = dict(store.db.execute("SELECT state,COUNT(*) FROM shared_writes WHERE project IS ? GROUP BY state", (project,)))
    rows = store.db.execute(
        "SELECT id,state,payload,result FROM shared_writes WHERE project IS ? ORDER BY rowid DESC LIMIT 15", (project,))
    jobs = []
    for row in rows:
        payload = json.loads(row["payload"])
        result = json.loads(row["result"] or "{}")
        jobs.append({"id": row["id"], "operation": payload["operation"], "source": payload["source"],
                     "state": row["state"], "version": result.get("version")})
    contributions = dict(store.db.execute(
        "SELECT COALESCE(json_extract(payload,'$.source.executor'),'Codex App'),COUNT(*) FROM turns "
        "WHERE json_extract(payload,'$.source.project') IS ? AND observed_revision IS NOT NULL GROUP BY 1", (project,)))
    deliveries = [{"executor": "Hermes" if str(chat).startswith("hermes:") else "Codex App", "name": name, "version": version}
                  for chat, name, version in store.db.execute(
        "SELECT json_extract(value,'$.source_event[0]'),json_extract(value,'$.name'),json_extract(value,'$.version') "
        "FROM meta WHERE key LIKE 'issued-delivery:%' AND json_extract(value,'$.project') IS ? ORDER BY rowid DESC LIMIT 10",
        (project,))]
    published = []
    for name, item in sorted(store.get("publications-v1", {}).items()):
        check = item.get("check", {})
        audience = check.get("audience", "public")
        if audience == "enterprise":
            if not active or check.get("boundary_id") != active["id"]:
                continue
        published.append({"name": name, "version": item["artifact"]["version"],
                          **({"audience": "enterprise"} if audience == "enterprise" else {})})
    return {"project": project, "writes": counts, "recent": jobs, "contributions": contributions,
            "deliveries": deliveries, "published": published, "protocol": VERSION}


def recover(bridge):
    if bridge.settings.shared_profile is not None:
        from .shared_storage import recover_memory_intents
        recover_memory_intents(bridge.settings, bridge.store)
        with bridge.store.db:
            bridge.store.db.execute(
                "UPDATE shared_writes SET state='uncertain',result=? WHERE state='applying'",
                (dumps({"error": "Owner stopped during a native operation; inspect the current knowledge before retrying"}),))


def drain(bridge):
    if bridge.settings.shared_profile is None:
        return
    from .shared_storage import apply_write
    rows = bridge.store.db.execute("SELECT * FROM shared_writes WHERE state='queued' ORDER BY rowid LIMIT 20").fetchall()
    for row in rows:
        with bridge.store.db:
            bridge.store.db.execute("UPDATE shared_writes SET state='applying' WHERE id=?", (row["id"],))
        try:
            state, result = apply_write(bridge, row["project"], json.loads(row["payload"]))
        except (ValueError, PermissionError, TypeError, KeyError) as exc:
            state, result = "failed", {"error": str(exc)}
        except Exception as exc:
            state, result = "uncertain", {"error": type(exc).__name__}
        with bridge.store.db:
            bridge.store.db.execute("UPDATE shared_writes SET state=?,result=? WHERE id=?",
                                    (state, dumps(redact(result)), row["id"]))
        bridge.last_publish = 0.0


def drain_client(bridge):
    profile = bridge.settings.shared_profile
    if profile is None:
        return
    from agent.shared_learning_client import OUTBOX, Refused, binding
    if not (profile / OUTBOX).exists():
        return
    client = binding(profile)
    if client is not None and hmac.compare_digest(client.client_id, bridge.settings.shared_client_id):
        def deliver(method, params):
            try:
                return rpc(bridge, "shared_" + method, params)
            except Rejected as exc:
                raise Refused(str(exc), permanent=True) from exc
        client.flush(deliver)


def review_scope(bridge, project):
    if not bridge.settings.uses_scoped_storage:
        return nullcontext()
    from .shared_storage import review_scope as scope
    return scope(bridge, project)


def rpc(bridge, method, params):
    settings = _binding(bridge, params)
    kind = method.removeprefix("shared_")
    if kind in {"event", "write"}:
        try:
            project = _project(settings, params.get("cwd"))
            with closing(Store(settings.state)) as store:
                if kind == "event":
                    return capture_native(settings, store, project, params)
                return enqueue(store, project, params, settings=settings)
        except (ValueError, PermissionError) as exc:
            raise Rejected(str(exc)) from exc
    project = _project(settings, params.get("cwd"))
    if kind == "hello":
        return {"state": "ready" if bridge.ready.is_set() and bridge.resume.is_set() else "starting",
                "protocol": VERSION, "project": project, "owner": "hermes_learning"}
    with closing(Store(settings.state)) as store:
        if kind == "receipt":
            return receipt(store, project, params.get("request_id"))
        if kind == "status":
            return status(store, project, settings=settings)
        if kind not in READS:
            raise ValueError("Unknown shared learning request")
        if not bridge.resume.is_set() or not bridge.ready.is_set():
            return {"available": False, "reason": "shared_owner_unavailable"}
        from .shared_storage import read
        return read(bridge, store, kind, project, params)
