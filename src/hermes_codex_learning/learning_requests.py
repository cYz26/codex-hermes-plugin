"""Durable, project-bound explicit requests; collection never invokes a model."""
from __future__ import annotations

import json
import re
import time

from .policy import admit
from .store import digest, dumps, redact

MAX_CHUNK_BYTES = 24 * 1024
MAX_CHUNKS = 64
TERMINAL = {"completed", "failed", "unknown", "cancelled", "no_evidence"}
METHODS = {"request_learning", "add_learning_material", "start_learning",
           "learning_request_status", "cancel_learning"}


def text(value, name, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode()) > limit:
        raise ValueError(f"{name} must be {'a possibly empty' if empty else 'a nonempty'} string of at most {limit} UTF-8 bytes")
    return value


def identity(value, name):
    text(value, name, 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise ValueError(f"Invalid {name}")
    return value


def admitted(settings, project, payload):
    assessment = admit(payload, settings.data_policy, project)
    if not assessment.get("allowed"):
        raise ValueError("Learning material refused: " + assessment.get("reason", "not_admitted"))
    value = assessment.get("payload", payload)
    if settings.data_policy.get("mode") == "trusted_enterprise":
        from .enterprise import scrub
        value = scrub(value)
    else:
        value = redact(value)
    return value, {k: v for k, v in assessment.items() if k != "payload"}


def get(store, project, request_id):
    identity(request_id, "request_id")
    row = store.db.execute("SELECT payload FROM learning_requests WHERE id=? AND project=?", (request_id, project)).fetchone()
    if row is None:
        raise ValueError("Learning request not found in this project")
    return json.loads(row[0])


def save(store, value):
    store.db.execute("UPDATE learning_requests SET payload=?,updated=? WHERE id=? AND project=?",
                     (dumps(value), time.time(), value["request_id"], value["project"]))


def materials(store, request_id):
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload FROM learning_materials WHERE request_id=? ORDER BY created,chunk_id", (request_id,))]


def view(store, value):
    result = {k: v for k, v in value.items() if k not in {"snapshot", "fingerprint", "assessment", "source", "batch_messages", "batch_focus"}}
    chunks = materials(store, value["request_id"])
    result["materials"] = [{k: v for k, v in c.items() if k not in {"content", "assessment"}} for c in chunks]
    result["progress"] = {"finished_batches": len(value.get("results", [])),
                          "total_batches": (len(chunks) + 1 if chunks else 0) if value["mode"] == "learn" else 1}
    result["effectiveness"] = "not_verified"
    return result


def chat_turns(store, project, chat):
    rows = store.db.execute("SELECT payload,observed_revision,revision FROM turns "
        "WHERE json_extract(payload,'$.source.project')=? AND json_extract(payload,'$.source.chat')=? ORDER BY rowid",
        (project, chat)).fetchall()
    return [(json.loads(row[0]), row[1] == row[2] and row[1] is not None) for row in rows]


def freeze(store, value):
    turns = chat_turns(store, value["project"], value["chat_id"])
    cutoff = value.get("through_turn") or value.get("cutoff_turn")
    if not cutoff:
        cutoff = next((turn["source"]["turn"] for turn, _ in reversed(turns)
                       if turn["status"] in {"completed", "interrupted"}), None)
        value["cutoff_turn"] = cutoff
    if cutoff:
        index = next((i for i, (turn, _) in enumerate(turns) if turn["source"]["turn"] == cutoff), None)
        if index is None:
            raise ValueError("through_turn is not a captured turn in this chat")
        turns = turns[:index + 1]
        target, observed = turns[-1]
        if target["status"] not in {"completed", "interrupted"} or not observed:
            value["state"] = "waiting_for_turn"
            return
    usable = [turn for turn, observed in turns if observed and turn["status"] in {"completed", "interrupted"}]
    if any(turn.get("_evidence_mode") == "workflow-v1" for turn in usable):
        raise ValueError("Explicit body learning needs admitted task evidence; this chat contains workflow-only evidence")
    value["snapshot"] = usable
    value["evidence"] = {"turns": [turn["source"]["turn"] for turn in usable], "snapshot_hash": digest(usable),
                         "coverage": sorted({gap for turn in usable for gap in turn.get("coverage", [])})}
    value["state"] = "queued" if usable else "no_evidence"


def submit(store, settings, project, params):
    allowed = {"cwd", "request_id", "chat_id", "mode", "focus", "scope", "through_turn", "skill_name", "memory_writes"}
    if set(params) - allowed:
        raise ValueError("Unknown learning request fields")
    request_id = identity(params.get("request_id"), "request_id")
    chat = identity(params.get("chat_id"), "chat_id")
    mode, scope = params.get("mode", "refine"), params.get("scope", "both")
    if mode not in {"refine", "learn"} or scope not in {"both", "memory", "skills"}:
        raise ValueError("Invalid learning mode or scope")
    if mode == "learn":
        scope = "skills"
    memory_writes = params.get("memory_writes", "add")
    if memory_writes not in {"add", "revise"} or (memory_writes == "revise" and scope == "skills"):
        raise ValueError("memory_writes must be add or revise; revise requires a memory scope")
    focus = text(params.get("focus", ""), "focus", 4000, empty=True)
    through = params.get("through_turn")
    if through is not None:
        identity(through, "through_turn")
    skill = params.get("skill_name")
    if mode == "learn" and (not isinstance(skill, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", skill)):
        raise ValueError("learn requires a safe native skill name")
    if mode == "refine" and skill is not None:
        raise ValueError("skill_name is only used in learn mode")
    contract = {"request_id": request_id, "project": project, "chat_id": chat, "mode": mode,
                "focus": focus, "scope": scope, "through_turn": through, "skill_name": skill, "memory_writes":memory_writes}
    fingerprint = digest(contract)
    existing = store.db.execute("SELECT payload FROM learning_requests WHERE id=?", (request_id,)).fetchone()
    if existing:
        value = json.loads(existing[0])
        if value["fingerprint"] != fingerprint:
            raise ValueError("request_id is already bound to another request")
        return view(store, value)
    if mode == "learn" and not re.fullmatch(r"learned-[a-z0-9][a-z0-9-]{0,55}", skill):
        # Reuse the worker's native catalog; collection must not instantiate a
        # second runtime or create a parallel learned-* copy of an existing skill.
        if skill not in store.get("artifact-read-cache", {}):
            raise ValueError("Use an existing native skill or a new learned-<topic> name")
    turns = chat_turns(store, project, chat)
    if not turns:
        raise ValueError("No captured chat matches chat_id in this project; wait for hooks or select an existing chat")
    value, assessment = admitted(settings, project, contract)
    if value != contract:
        raise ValueError("Request instructions were transformed by admission; submit the admitted instructions explicitly")
    source = turns[-1][0]["source"]
    value.update(fingerprint=fingerprint, assessment=assessment,
                 source={key: source[key] for key in ("host", "project", "chat")},
                 state="collecting", results=[], created_at=time.time(), batch=0)
    if mode == "refine":
        freeze(store, value)
    store.db.execute("INSERT INTO learning_requests VALUES (?,?,?,?,?,?)",
                     (request_id, project, chat, dumps(value), value["created_at"], value["created_at"]))
    return view(store, value)


def add_material(store, settings, project, params):
    if set(params) - {"cwd", "request_id", "chunk_id", "source", "title", "content"}:
        raise ValueError("Unknown material fields")
    value = get(store, project, params["request_id"])
    if value["mode"] != "learn":
        raise ValueError("Materials require a learn request")
    chunk_id = identity(params.get("chunk_id"), "chunk_id")
    chunk = {"chunk_id": chunk_id, "source": text(params.get("source"), "source", 512),
             "title": text(params.get("title", chunk_id), "title", 160),
             "content": text(params.get("content"), "content", MAX_CHUNK_BYTES)}
    chunk, assessment = admitted(settings, project, chunk)
    chunk["sha256"] = digest(chunk)
    chunk["reference"] = "references/" + digest([value["request_id"], chunk_id])[:16] + ".md"
    existing = store.db.execute("SELECT payload FROM learning_materials WHERE request_id=? AND chunk_id=?",
                                (value["request_id"], chunk_id)).fetchone()
    if existing:
        if json.loads(existing[0])["sha256"] != chunk["sha256"]:
            raise ValueError("chunk_id is already bound to different material")
        return view(store, value)
    if value["state"] != "collecting":
        raise ValueError("Materials can only be added to a collecting learn request")
    if len(materials(store, value["request_id"])) >= MAX_CHUNKS:
        raise ValueError(f"A request supports at most {MAX_CHUNKS} chunks; continue in a new request")
    chunk["assessment"] = assessment
    store.db.execute("INSERT INTO learning_materials VALUES (?,?,?,?)",
                     (value["request_id"], chunk_id, dumps(chunk), time.time()))
    return view(store, value)


def rpc(store, settings, project, method, params):
    if method == "learning_request_status":
        return view(store, get(store, project, params["request_id"]))
    # Serialize short collection/state transactions across MCP/CLI connections.
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        if method == "request_learning":
            return submit(store, settings, project, params)
        if method == "add_learning_material":
            return add_material(store, settings, project, params)
        value = get(store, project, params["request_id"])
        if method == "start_learning":
            if value["mode"] != "learn":
                raise ValueError("refine requests start automatically")
            if value["state"] == "collecting":
                if not materials(store, value["request_id"]):
                    raise ValueError("Add at least one material chunk before starting")
                value["state"] = "queued"
        elif method == "cancel_learning":
            if value["state"] not in TERMINAL:
                value["state"] = "cancel_requested" if value.get("intent_id") else "cancelled"
                value["cancellation"] = "Stops future batches; an in-flight batch may finish. Existing writes are retained."
        else:
            raise ValueError("Unknown learning request method")
        save(store, value)
        return view(store, value)


def retire(store, cutoff, *, dry_run=True):
    """Retain identities/outcomes while retiring completed request bodies after retention."""
    rows = store.db.execute("SELECT payload FROM learning_requests WHERE updated<? AND "
        "json_extract(payload,'$.state') IN ('completed','failed','cancelled','no_evidence') AND "
        "json_extract(payload,'$.body_retired') IS NULL LIMIT 1000", (cutoff,)).fetchall()
    if not dry_run:
        with store.db:
            for row in rows:
                value = json.loads(row[0])
                for field in ("snapshot", "batch_messages", "batch_focus", "focus"):
                    value.pop(field, None)
                for result in value["results"]:
                    for field in ("applied", "staged", "uncertain"):
                        result[field + "_count"] = len(result.pop(field, []))
                for chunk in materials(store, value["request_id"]):
                    chunk.pop("content", None)
                    chunk["body_retired"] = True
                    store.db.execute("UPDATE learning_materials SET payload=? WHERE request_id=? AND chunk_id=?",
                                     (dumps(chunk), value["request_id"], chunk["chunk_id"]))
                value["body_retired"] = True
                save(store, value)
    return {"eligible_requests":len(rows), "retired_requests":0 if dry_run else len(rows)}
