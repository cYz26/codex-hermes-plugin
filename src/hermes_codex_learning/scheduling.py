"""Durable high-value signals and fair ordering; no model calls in collection."""
from __future__ import annotations

import re
import time

from .store import digest
from .workflow import action

CORRECTION = re.compile(r"(?im)^\s*(?:correction\s*:|actually\b|更正[：:]?|纠正[：:]?|不对[，,：:]|应该改为)")


def annotate(payload: dict, store) -> dict:
    payload = dict(payload)
    chat, turn = payload.get("session_id"), payload.get("turn_id")
    if chat and turn and payload.get("hook_event_name") == "UserPromptSubmit":
        if CORRECTION.search(str(payload.get("prompt", ""))[:65536]):
            with store.db:
                store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                                 (chat, turn, "user_correction", time.time()))
    if payload.get("hook_event_name") in {"PreToolUse", "PostToolUse"}:
        payload["bridge_action"] = action(payload)
        args = payload.get("tool_input")
        if payload["bridge_action"] == "test" and isinstance(args, dict):
            payload["bridge_test_key"] = digest([payload.get("cwd"), args.get("cmd") or args.get("command")])
    return payload


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
    # High-value candidates still wait for real terminal execution facts.
    if data["status"] != "completed" or any(tool["status"] in {"running", "unknown"} for tool in data["tools"].values()):
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
        return (0 if now - age >= 1800 else 1 if intent["handle"] in priorities else 2, age)
    return sorted(intents, key=rank)
