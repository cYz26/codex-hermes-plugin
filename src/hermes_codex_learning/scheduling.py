"""Durable high-value signals and fair ordering; no model calls in collection."""
from __future__ import annotations

import json
import re
import time

from . import native_reads
from .claude_hooks import EXECUTOR as CLAUDE, has_test_result
from .normalize import event_tool_state
from .store import digest

CORRECTION = re.compile(r"(?i)(?:correction[ \t]*[:：]|(?:更正|纠正)(?:一下)?[：:，,]|不对[，,：:]|应该改为)")
AUTOMATIC = {"native_threshold", "idle_flush"}
REVIEW_REASONS = AUTOMATIC | {"retry"}
SETTLE_SECONDS = 15
MAX_WAIT_SECONDS = 30 * 60


def is_explicit_correction(prompt):
    return isinstance(prompt, str) and bool(CORRECTION.match(prompt[:65536].lstrip()))


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
        if is_explicit_correction(payload.get("prompt")):
            store.db.execute("INSERT OR IGNORE INTO learning_signals VALUES (?,?,?,?)",
                             (chat, turn, "user_correction", time.time()))


def enqueue(store, data: dict, handle: str, *, recovered_terminal=False):
    source = data["source"]
    # The hook may belong to an older installed client. Base scheduling on the
    # observed text instead of the client's historical correction label.
    signals = set()
    if is_explicit_correction(data.get("user")):
        signals.add("user_correction")
    if recovered_terminal:
        signals.add("terminal_evidence_recovered")
    if data["status"] == "completed" and same_test_recovered(store, data):
        signals.add("same_test_recovered")
    if not signals:
        return
    # Dialogue lessons do not assert that unresolved tools succeeded. Execution
    # recovery still requires the real matching test result.
    dialogue = "user_correction" in signals
    if data["status"] != "completed" or (not dialogue and any(
            tool["status"] in {"running", "unknown"} for tool in data["tools"].values())):
        return
    identity, revision = digest([source["chat"], source["turn"]]), digest(data)
    with store.db:
        store.db.execute("INSERT INTO review_priority VALUES (?,?,?,?,?,NULL) "
                         "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,handle=excluded.handle,"
                         "requested=CASE WHEN review_priority.revision=excluded.revision THEN requested ELSE NULL END",
                         (identity, handle, revision, ",".join(sorted(signals)), time.time()))


def same_test_recovered(store, data):
    from .host_evidence import _at

    tools = data["tools"]
    failed_keys = {item["test_key"] for item in tools.values()
                   if item.get("test_key") and item["status"] == "failed"}
    if not any(item.get("test_key") in failed_keys and item["status"] == "completed" for item in tools.values()):
        return False
    source = data["source"]
    rows = store.db.execute(
        "SELECT rowid,created,payload FROM events WHERE json_extract(payload,'$.session_id')=? "
        "AND json_extract(payload,'$.turn_id')=? AND json_extract(payload,'$.hook_event_name') "
        "IN ('PreToolUse','PostToolUse') ORDER BY created,rowid",
        (source["chat"], source["turn"]),
    )
    started, finished = {}, {}
    for row in rows:
        event = json.loads(row["payload"])
        call = event.get("tool_use_id")
        tool = tools.get(call)
        if (tool is None or event.get("bridge_host") != source["host"]
                or event.get("bridge_project") != source["project"]):
            continue
        position = row["created"], row["rowid"]
        if event["hook_event_name"] == "PreToolUse":
            started.setdefault(call, position)
            continue
        response = event.get("tool_response")
        if event.get("bridge_evidence_mode") == "workflow-v1":
            status = response.get("outcome") if isinstance(response, dict) else None
        else:
            status = event_tool_state(event)[0]
        if source.get("executor") == CLAUDE and tool.get("test_key") and not has_test_result(
                status, event.get("bridge_tool_status_source")):
            continue
        # A receipt from the original launch of a background command cannot
        # borrow the terminal status of a later poll.
        if status in {"failed", "completed"} and status == tool["status"]:
            finished.setdefault(call, position)
    for call, tool in tools.items():
        if tool.get("status_source") == "host_transcript" and tool["status"] in {"failed", "completed"}:
            at = _at(tool.get("host_position", {}))
            if at is not None and (at, 0) > started.get(call, (0, 0)):
                finished[call] = at, 0

    failed = {}
    last_edit = (0, 0)
    for call, position in sorted(finished.items(), key=lambda item: item[1]):
        tool = tools[call]
        status = tool["status"]
        if tool.get("action") == "edit" and status == "completed":
            last_edit = position
        key = tool.get("test_key")
        if key and status == "failed":
            failed[key] = position
        elif key in failed and status == "completed" and failed[key] < last_edit < started.get(call, (0, 0)) < position:
            return True
    return False


def flush(store, api, *, recovery_only=False):
    # Older bridges persisted ordinary followups as priority work. Keep the
    # dialogue evidence, but do not use those rows to create review intents.
    pending = "requested IS NULL AND reason<>'user_followup'"
    if recovery_only:
        pending += " AND instr(',' || reason || ',', ',terminal_evidence_recovered,') > 0"
    rows = store.db.execute(
        f"SELECT handle FROM review_priority WHERE {pending} GROUP BY handle ORDER BY MIN(created) LIMIT 20"
    ).fetchall()
    for row in rows:
        handle = row["handle"]
        targets = [store.get("observation-receipt:" + item[0], {}).get("revision") for item in store.db.execute(
            f"SELECT id FROM review_priority WHERE handle=? AND {pending}", (handle,)
        )]
        target = max((revision for revision in targets if type(revision) is int), default=None)
        reviewed = native_reads.reviewed_revision(api, handle)
        if target is None or reviewed is None or reviewed < target:
            result = api.maintenance(handle=handle, idle_flush=True)
            if not (result and result.get("intent")) and target is not None and callable(getattr(api, "read", None)):
                intents = native_reads.status(api).get("intents", [])
                if not any(item.get("handle") == handle and item.get("status") in {"queued", "running"}
                           and item.get("reason") != "user_request" and item.get("revision", -1) >= target
                           for item in intents):
                    continue  # Guarded by an older revision, or an unconfirmed outcome.
        with store.db:
            store.db.execute(f"UPDATE review_priority SET requested=? WHERE handle=? AND {pending}",
                             (time.time(), handle))


def ordered_intents(store, intents):
    now = time.time()
    priorities = {row[0] for row in store.db.execute("SELECT DISTINCT handle FROM review_priority WHERE reason<>'user_followup'")}
    queued = [intent for intent in intents if intent.get("status") == "queued"]
    ages = {}
    for intent in queued:
        key = "intent-seen:" + intent["id"]
        first_seen = store.get(key)
        if first_seen is None:
            first_seen = now
            store.set(key, first_seen)
        ages[intent["id"]] = first_seen

    groups, candidates, unconfirmed = {}, [], {}
    for intent in intents:
        if (intent.get("status") == "unknown" and intent.get("reason") in REVIEW_REASONS
                and type(intent.get("revision")) is int):
            unconfirmed.setdefault(intent["handle"], []).append(intent)
    for intent in queued:
        if intent.get("reason") in REVIEW_REASONS and type(intent.get("revision")) is int:
            groups.setdefault(intent["handle"], []).append(intent)
        else:
            candidates.append((intent, ages[intent["id"]]))

    for handle, group in groups.items():
        group.sort(key=lambda item: item["revision"])
        scopes = set()
        selected = None
        for item in group:
            writes = {scope for scope in ("memory", "skills") if item.get(scope)}
            scopes.update(writes)
            # Superseding an earlier intent also advances the native watermark
            # for that revision. Only do it if all requested scopes are covered.
            if scopes <= writes and not any(
                item["revision"] <= prior["revision"]
                and writes <= {scope for scope in ("memory", "skills") if prior.get(scope)}
                for prior in unconfirmed.get(handle, ())
            ):
                selected = item
            if item.get("reason") == "retry":
                break  # Preserve the native recovery snapshot and retry policy.
        if selected is None:
            continue
        first_seen = min(ages[item["id"]] for item in group if item["revision"] <= selected["revision"])
        if (selected.get("reason") != "retry" and now - first_seen < MAX_WAIT_SECONDS
                and chat_is_active(store, handle, now)):
            continue
        candidates.append((selected, first_seen))

    def rank(candidate):
        intent, first_seen = candidate
        urgent = intent.get("reason") == "user_request" or intent["handle"] in priorities
        return (0 if now - first_seen >= MAX_WAIT_SECONDS else 1 if urgent else 2, first_seen)

    return [intent for intent, _ in sorted(candidates, key=rank)]


def chat_is_active(store, handle, now):
    source = store.get("workflow-local-source:" + handle) or store.get("scope:" + handle, {})
    row = store.db.execute(
        "SELECT t.activity, json_extract(t.payload, '$.status') status "
        "FROM latest_turns l JOIN turns t ON t.id=l.turn_id WHERE l.chat=?",
        (source.get("chat"),),
    ).fetchone()
    return bool(row and (row["status"] == "running" or now - row["activity"] < SETTLE_SECONDS))
