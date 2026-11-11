"""Version-bound application claims; host tests do not establish causation."""
from __future__ import annotations

import json
import time

from .store import digest, dumps


def record(store, token, project, stage, events):
    issued = store.get("issued-delivery:" + token)
    if not issued or issued.get("project") != project:
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
             "validation":"pending" if stage != "applied" else "not_supplied", "effectiveness":"not_inferred"}
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO application_evidence VALUES (?,?,?)", (identity,dumps(value),time.time()))
    reconcile(store)
    return json.loads(store.db.execute("SELECT payload FROM application_evidence WHERE id=?", (identity,)).fetchone()[0])


def reconcile(store):
    rows = store.db.execute("SELECT id,payload FROM application_evidence WHERE "
                           "json_extract(payload,'$.validation')='pending' ORDER BY created LIMIT 100").fetchall()
    for row in rows:
        value = json.loads(row["payload"])
        outcomes = []
        for event in value["events"]:
            turn = store.db.execute("SELECT payload FROM turns WHERE id=?", (digest([event["chat"],event["turn"]]),)).fetchone()
            if not turn:
                continue
            data = json.loads(turn[0])
            tool = data["tools"].get(event["tool_call"], {})
            if data["source"]["project"] == value["project"] and tool.get("action") == "test":
                outcomes.append(tool.get("status"))
        if len(outcomes) != len(value["events"]) or not outcomes or any(x in {"running","unknown"} for x in outcomes):
            continue
        value["validation"] = "host_test_passed" if all(x == "completed" for x in outcomes) else "host_test_failed"
        with store.db:
            store.db.execute("UPDATE application_evidence SET payload=? WHERE id=?", (dumps(value),row["id"]))
            if value["validation"] == "host_test_failed":
                store.db.execute("INSERT OR REPLACE INTO withheld_versions VALUES (?,?,?)",
                                 (value["name"],value["version"],row["id"]))
                for event in value["events"]:
                    store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                                     (event["chat"],event["turn"],"skill_validation_failed",time.time()))
