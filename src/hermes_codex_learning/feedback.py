"""Version-bound application claims; host tests do not establish causation."""
from __future__ import annotations

import json
import time

from .normalize import _execution_result
from .store import digest, dumps
from .workflow import MODE, action


def _delivery(store, token, project):
    issued = store.get("issued-delivery:" + token)
    if issued and issued.get("project") == project:
        return issued
    # The hook can have confirmed a load while the native owner is in a review.
    # The signed outbox receipt is already enough to identify the delivered bytes.
    from .read_receipts import PREFIX, verify
    if not token.startswith(PREFIX):
        return None
    row = store.db.execute("""
        SELECT payload FROM delivery_events WHERE json_extract(payload,'$.bridge_project')=?
        AND json_extract(payload,'$.tool_response.structuredContent.delivery_token')=?
        ORDER BY rowid LIMIT 1
    """, (project, token)).fetchone()
    if not row:
        return None
    event = json.loads(row[0])
    signed = verify(store.root, token, project)
    delivered = event["tool_response"]["structuredContent"]
    if any(delivered.get(key) != signed[key] for key in ("name", "version")):
        return None
    return {**signed, "source_event": [event["session_id"], event["turn_id"], event["tool_use_id"]]}


def _is_test(tool):
    arguments = tool.get("arguments")
    if isinstance(arguments, dict) and (arguments.get("command") or arguments.get("cmd")):
        # Reclassify retained commands from before action metadata was captured.
        return action({"tool_name": tool.get("name"), "tool_input": arguments}) == "test"
    return tool.get("action") == "test"


def _test_outcome(tool, evidence_mode):
    status = tool.get("status")
    if evidence_mode == MODE:
        return status if tool.get("name") == "workflow_test" else None
    if status not in {"completed", "failed"} or not _is_test(tool):
        return None
    # A generic host completion or transport failure does not supply an exit code.
    result, unknown = _execution_result(tool.get("result"))
    if unknown or not isinstance(result, dict) or type(result.get("exit_code")) is not int:
        return None
    if result.get("interrupted") is True:
        return None
    expected = "completed" if result["exit_code"] == 0 else "failed"
    return status if status == expected else None


def _outcome_source(tool, evidence_mode):
    result, _ = _execution_result(tool.get("result"))
    if tool.get("status_source") == "host_transcript" or (
            isinstance(result, dict) and result.get("status_source") == "host_transcript"):
        return "host_transcript"
    return "workflow_projection" if evidence_mode == MODE else "hook_exit_code"


def test_references(store, project, chat, turn):
    """List captured test references without returning commands or output."""
    captured = store.db.execute("""
        SELECT json_extract(payload,'$.tool_use_id') AS tool_id
        FROM events
        WHERE json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.turn_id')=?
          AND json_extract(payload,'$.bridge_project')=? AND json_extract(payload,'$.bridge_action')='test'
          AND json_extract(payload,'$.hook_event_name')='PostToolUse' AND tool_id IS NOT NULL
        GROUP BY tool_id ORDER BY MAX(rowid) DESC LIMIT 20
    """, (chat, turn, project))
    outcomes = {row[0]: {"outcome": "pending"} for row in captured}
    rows = store.db.execute("""
        SELECT tool.key, tool.value, json_extract(t.payload,'$._evidence_mode')
        FROM turns t JOIN json_each(t.payload,'$.tools') tool
        WHERE t.id=? AND json_extract(t.payload,'$.source.project')=?
    """, (digest([chat, turn]), project))
    for tool_id, raw, evidence_mode in rows:
        tool = json.loads(raw)
        recognized = tool.get("name") == "workflow_test" if evidence_mode == MODE else _is_test(tool)
        if not recognized:
            continue
        outcome = _test_outcome(tool, evidence_mode)
        outcomes[tool_id] = {"outcome": outcome if outcome in {"completed", "failed"} else "pending"}
        if outcome in {"completed", "failed"}:
            outcomes[tool_id]["outcome_source"] = _outcome_source(tool, evidence_mode)
    return [{"source_event": {"chat": chat, "turn": turn, "tool_call": tool_id}, **result}
            for tool_id, result in list(outcomes.items())[:20]]


def deliveries_for_turn(store, project, chat, turn, *, limit=None):
    query = """SELECT json_extract(value,'$.name'), json_extract(value,'$.version') FROM meta
        WHERE key LIKE 'issued-delivery:%' AND json_extract(value,'$.project')=?
          AND json_extract(value,'$.source_event[0]')=? AND json_extract(value,'$.source_event[1]')=?
    """
    args = (project, chat, turn)
    if limit is not None:
        query += " ORDER BY rowid DESC LIMIT ?"
        args += (limit,)
    return [{"name": name, "version": version} for name, version in store.db.execute(query, args)]


def applications_for_turn(store, project, chat, turn, *, limit=None):
    query = """SELECT payload FROM application_evidence WHERE json_extract(payload,'$.project')=?
        AND ((json_extract(payload,'$.delivery_source_event[0]')=? AND json_extract(payload,'$.delivery_source_event[1]')=?)
        OR EXISTS (SELECT 1 FROM json_each(payload,'$.events') ref WHERE
            json_extract(ref.value,'$.chat')=? AND json_extract(ref.value,'$.turn')=?))
    """
    args = (project, chat, turn, chat, turn)
    if limit is not None:
        query += " ORDER BY rowid DESC LIMIT ?"
        args += (limit,)
    return [json.loads(row[0]) for row in store.db.execute(query, args)]


def status(store, project, chat, turn=None, receipt_id=None):
    if not isinstance(chat, str) or not chat or len(chat) > 256 or (
            turn is not None and (not isinstance(turn, str) or not turn or len(turn) > 256)):
        raise ValueError("Feedback status requires a valid chat and optional turn")
    if receipt_id is not None:
        if not isinstance(receipt_id, str) or len(receipt_id) != 64:
            raise ValueError("Feedback status requires a valid receipt ID")
        row = store.db.execute("SELECT payload FROM application_evidence WHERE id=?", (receipt_id,)).fetchone()
        receipt = json.loads(row[0]) if row else {}
        sources = [receipt.get("delivery_source_event") or []]
        sources.extend([item["chat"], item["turn"]] for item in receipt.get("events", []))
        if receipt.get("project") != project or not any(
                source and source[0] == chat and (turn is None or source[1] == turn) for source in sources):
            return {"state": "receipt_unavailable"}
        return {"state": "available", "receipt": receipt}
    if turn is None:
        row = store.db.execute("""
            SELECT json_extract(payload,'$.turn_id') FROM events
            WHERE json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.bridge_project')=?
              AND json_extract(payload,'$.turn_id') IS NOT NULL
            GROUP BY json_extract(payload,'$.turn_id') ORDER BY MIN(rowid) DESC LIMIT 1
        """, (chat, project)).fetchone()
        turn = row[0] if row else None
    if turn is None:
        return {"state": "awaiting_capture", "chat": chat, "test_references": [], "applications": []}
    row = store.db.execute("""
        SELECT json_extract(payload,'$.status') FROM turns WHERE id=? AND json_extract(payload,'$.source.project')=?
    """, (digest([chat, turn]), project)).fetchone()
    applications = applications_for_turn(store, project, chat, turn, limit=5)
    fields = ("id", "name", "version", "application", "validation", "validation_reason", "superseded_by")
    return {"state": row[0] if row else "awaiting_ingestion", "chat": chat, "turn": turn,
            "test_references": test_references(store, project, chat, turn),
            "deliveries": deliveries_for_turn(store, project, chat, turn, limit=10),
            "applications": [{key: item[key] for key in fields if key in item} for item in applications]}


def record(store, token, project, stage, events):
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ValueError("Invalid skill delivery receipt")
    issued = _delivery(store, token, project)
    if not issued:
        raise PermissionError("Application evidence requires a delivery in this project")
    if stage not in {"applied","verified","failed"} or not isinstance(events, list) or len(events) > 20:
        raise ValueError("Invalid application evidence")
    for event in events:
        if not isinstance(event, dict) or set(event) != {"chat","turn","tool_call"} or any(
                not isinstance(v, str) or not v or len(v) > 256 for v in event.values()):
            raise ValueError("Use host event references, not task text")
    identity = digest([token, stage, events])
    value = {"id":identity, "name":issued["name"], "version":issued["version"], "project":project,
             "requested":stage, "events":events, "application":"self_reported",
             "delivery_source_event":issued.get("source_event"),
             "validation":"pending" if stage != "applied" else "not_supplied", "effectiveness":"not_inferred"}
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO application_evidence VALUES (?,?,?)", (identity,dumps(value),time.time()))
    reconcile(store, identifier=identity)
    return json.loads(store.db.execute("SELECT payload FROM application_evidence WHERE id=?", (identity,)).fetchone()[0])


def _test_evidence(store, project, event):
    row = store.db.execute("SELECT payload FROM turns WHERE id=?",
                           (digest([event["chat"], event["turn"]]),)).fetchone()
    if not row:
        return "awaiting_ingestion", None
    data = json.loads(row[0])
    if data["source"]["project"] != project:
        return "reference_outside_project", None
    if data.get("status") == "retired":
        return "evidence_retired", None
    tool = data["tools"].get(event["tool_call"])
    if tool and (outcome := _test_outcome(tool, data.get("_evidence_mode"))) in {"completed", "failed"}:
        return outcome, _outcome_source(tool, data.get("_evidence_mode"))
    if store.db.execute("""
        SELECT 1 FROM events WHERE json_extract(payload,'$.session_id')=?
          AND json_extract(payload,'$.turn_id')=? AND json_extract(payload,'$.bridge_project')=?
          AND json_extract(payload,'$.tool_use_id')=? AND delivered IS NULL LIMIT 1
    """, (event["chat"], event["turn"], project, event["tool_call"])).fetchone():
        return "awaiting_ingestion", None
    if tool and tool.get("status") == "running":
        return "awaiting_host_completion", None
    if data.get("_ended_by") not in {"Stop", "Interrupt"} and data.get("status") not in {"completed", "interrupted"}:
        return "awaiting_host_completion", None
    if not tool:
        return "reference_not_found", None
    if not (_is_test(tool) or data.get("_evidence_mode") == MODE and tool.get("name") == "workflow_test"):
        return "reference_is_not_a_test", None
    return "exit_status_unavailable", None


def _correction(store, row, value):
    delivery = value.get("delivery_source_event")
    reasons = set(value.get("validation_reasons", []))
    if not delivery or not reasons or reasons - {"reference_not_found", "reference_is_not_a_test"}:
        return None
    affected_turns = {(event["chat"], event["turn"]) for event in value["events"]}
    for replacement, raw in store.db.execute("""
        SELECT id, payload FROM application_evidence WHERE rowid>?
          AND json_extract(payload,'$.delivery_source_event')=? AND json_extract(payload,'$.requested')=?
          AND json_extract(payload,'$.project')=? AND json_extract(payload,'$.version')=?
          AND json_extract(payload,'$.validation') IN ('host_test_passed','host_test_failed')
        ORDER BY rowid DESC
    """, (row["rowid"], dumps(delivery), value["requested"], value["project"], value["version"])):
        events = json.loads(raw)["events"]
        if {(event["chat"], event["turn"]) for event in events} == affected_turns:
            return replacement
    return None


def reconcile(store, *, identifier=None):
    pending = "SELECT rowid,id,payload FROM application_evidence WHERE json_extract(payload,'$.validation')='pending'"
    if identifier is None:
        cursor = store.get("application-feedback-cursor", 0)
        rows = store.db.execute(pending + " AND rowid>? ORDER BY rowid LIMIT 100", (cursor,)).fetchall()
    else:
        rows = store.db.execute(pending + " AND id=?", (identifier,)).fetchall()
    for row in rows:
        value = json.loads(row["payload"])
        evidence = [_test_evidence(store, value["project"], event) for event in value["events"]]
        outcomes = [outcome for outcome, _ in evidence]
        unresolved = [outcome for outcome in outcomes if outcome not in {"completed", "failed"}]
        if unresolved or not outcomes:
            value["validation_reason"] = unresolved[0] if unresolved else "no_test_references"
            value["validation_reasons"] = sorted(set(unresolved)) or ["no_test_references"]
            if replacement := _correction(store, row, value):
                value.update(validation="superseded", superseded_by=replacement)
        else:
            value["validation"] = "host_test_passed" if all(x == "completed" for x in outcomes) else "host_test_failed"
            value["test_outcomes"] = [{"source_event": event, "outcome": outcome, "outcome_source": source}
                                      for event, (outcome, source) in zip(value["events"], evidence)]
            value.pop("validation_reason", None)
            value.pop("validation_reasons", None)
        serialized = dumps(value)
        if serialized == row["payload"]:
            continue
        with store.db:
            updated = store.db.execute("UPDATE application_evidence SET payload=? WHERE id=? "
                                       "AND payload=?", (serialized, row["id"], row["payload"]))
            if updated.rowcount and value["validation"] == "host_test_failed":
                store.db.execute("INSERT OR REPLACE INTO withheld_versions VALUES (?,?,?)",
                                 (value["name"],value["version"],row["id"]))
                for event in value["events"]:
                    store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                                     (event["chat"],event["turn"],"skill_validation_failed",time.time()))
    if identifier is None and (cursor or rows):
        store.set("application-feedback-cursor", rows[-1]["rowid"] if len(rows) == 100 else 0)
