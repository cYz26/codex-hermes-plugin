"""Durable high-value signals and fair ordering; no model calls in collection."""
from __future__ import annotations

import re
import time

from .store import digest

CORRECTION = re.compile(r"(?im)^\s*(?:correction\s*:|actually\b|更正[：:]?|纠正[：:]?|不对[，,：:]|应该改为)")


def record_dialogue_signals(payload: dict, store) -> None:
    chat, turn = payload.get("session_id"), payload.get("turn_id")
    if not chat or not turn:
        return
    previous = store.db.execute("SELECT 1 FROM events WHERE json_extract(payload,'$.session_id')=? "
                                "AND json_extract(payload,'$.turn_id')<>? "
                                "AND json_extract(payload,'$.hook_event_name')='UserPromptSubmit' LIMIT 1",
                                (chat, turn)).fetchone()
    relation = payload.get("bridge_host_context", {}).get("relation", {})
    with store.db:
        if previous or relation.get("status") == "verified":
            store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                             (chat, turn, "user_followup", time.time()))
        if CORRECTION.search(str(payload.get("prompt", ""))[:65536]):
            store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                             (chat, turn, "user_correction", time.time()))


def enqueue(store, data: dict, handle: str):
    source = data["source"]
    signals = {row[0] for row in store.db.execute("SELECT kind FROM learning_signals WHERE chat=? AND turn=?",
                                                 (source["chat"], source["turn"]))}
    failed, edited = set(), False
    for tool in data["tools"].values():
        if tool.get("action") == "edit" and failed:
            edited = True
        key = tool.get("test_key")
        if key and tool["status"] == "failed":
            failed.add(key)
        elif key in failed and edited and tool["status"] == "completed":
            signals.add("same_test_recovered")
    if not signals:
        return
    # Dialogue lessons do not assert that unresolved tools succeeded. Execution
    # recovery still requires the real matching test result.
    dialogue = bool(signals & {"user_followup", "user_correction"})
    if data["status"] != "completed" or (not dialogue and any(
            tool["status"] in {"running", "unknown"} for tool in data["tools"].values())):
        return
    identity, revision = digest([source["chat"], source["turn"]]), digest(data)
    with store.db:
        store.db.execute("INSERT INTO review_priority VALUES (?,?,?,?,?,NULL) "
                         "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,handle=excluded.handle,"
                         "requested=CASE WHEN review_priority.revision=excluded.revision THEN requested ELSE NULL END",
                         (identity, handle, revision, ",".join(sorted(signals)), time.time()))


def flush(store, api):
    rows = store.db.execute("SELECT * FROM review_priority WHERE requested IS NULL ORDER BY created LIMIT 20").fetchall()
    seen = set()
    for row in rows:
        if row["handle"] in seen:
            continue
        seen.add(row["handle"])
        api.maintenance(handle=row["handle"], idle_flush=True)
        targets=[store.get("observation-receipt:"+item[0],{}).get("revision") for item in store.db.execute(
            "SELECT id FROM review_priority WHERE handle=? AND requested IS NULL",(row["handle"],))]
        known=[revision for revision in targets if type(revision) is int]
        if known and callable(getattr(api,"read",None)):
            from .native_reads import status
            intents=status(api).get("intents",[])
            if not any(intent.get("handle")==row["handle"] and intent.get("status") in {"queued","running"}
                       and intent.get("revision",-1)>=max(known) for intent in intents):
                # Native idle flush may defer while an older revision is queued.
                # Keep the feedback pending so it is retried after that review.
                continue
        with store.db:
            store.db.execute("UPDATE review_priority SET requested=? WHERE handle=? AND requested IS NULL",
                             (time.time(), row["handle"]))


def ordered_intents(store, intents):
    priorities = {row[0]:row[1] for row in store.db.execute("SELECT handle,MIN(created) FROM review_priority GROUP BY handle")}
    now = time.time()
    # Persist first-seen age even for ordinary native intents, so they cannot starve.
    for intent in intents:
        key = "intent-seen:" + intent["id"]
        if store.get(key) is None:
            store.set(key, now)
    def rank(intent):
        age = store.get("intent-seen:" + intent["id"], now)
        return (0 if now - age >= 1800 else 1 if intent["handle"] in priorities or intent.get("reason") == "user_request" else 2, age)
    return sorted(intents, key=rank)
