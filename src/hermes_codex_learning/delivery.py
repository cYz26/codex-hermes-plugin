"""Technical delivery outbox, independent of task-body admission."""
from __future__ import annotations

import json

from .store import digest, dumps


def capture_delivery(store, payload: dict, project: str) -> None:
    if (payload.get("hook_event_name") != "PostToolUse"
            or "hermes_learning" not in payload.get("tool_name", "")
            or not payload.get("tool_name", "").endswith("skill_load")
            or not all(isinstance(payload.get(key), str) and payload[key]
                       for key in ("session_id", "turn_id", "tool_use_id"))):
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
