"""UI-only durable notification transport. This module never calls a model or MCP."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .store import atomic_json, digest, dumps


def entities(facts: list[dict]) -> list[str]:
    result = set()
    for fact in facts:
        operation = fact.get("operation") or fact
        if fact.get("tool") == "skill_manage":
            children = fact.get("result", {}).get("results", []) or [operation]
            for child in children:
                name = child.get("name") or operation.get("name")
                if name:
                    result.add("skill:" + name)
        elif fact.get("tool") == "memory":
            # Without a native entry ID, count distinct declared final contents,
            # not the number of file writes. Exact before/after entries override
            # this fallback on live review events.
            text = operation.get("content") or operation.get("new_text")
            if text:
                result.add("memory:" + digest([operation.get("target"), text]))
            else:
                result.add("memory-target:" + str(operation.get("target") or fact.get("result", {}).get("target", "memory")))
    return sorted(result)


class NoticeQueue:
    def __init__(self, state: Path):
        self.state = state
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(state / "notifications.sqlite", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, scope TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(consumer TEXT, id TEXT, stage TEXT, at REAL,
            PRIMARY KEY(consumer,id,stage));
        """)
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS notice_order(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE)")
                self.db.execute("INSERT OR IGNORE INTO notice_order(id) SELECT id FROM notices ORDER BY created,id")
                self.db.execute("PRAGMA user_version=2")

    def close(self):
        self.db.close()

    def publish(self, event: dict) -> None:
        from .notify import should_notify, persist_receipt
        if not should_notify(event):
            return
        if event["kind"] == "publication.applied":
            event = {"id": event["id"], "kind": event["kind"], "name": event["name"],
                     "message": "Hermes 已发布 1 项通用技能：" + event["name"],
                     "receipt": "通用技能：" + event["name"] + "\n共享检查与发布：通过"}
        scope = event.get("project") or ("global" if event["kind"] == "publication.applied" else "system")
        applied = event.get("applied_entities", entities(event.get("applied", [])))
        staged = entities(event.get("staged", []))
        if event["kind"] == "publication.applied":
            applied = ["skill:" + event["name"]]
        elif event["kind"] == "publication.pending":
            staged = ["skill:" + event["name"]]
        incomplete = event.get("run_status") in {"failed", "interrupted", "unknown"}
        status = "partial" if incomplete and applied else "failed" if incomplete else "applied" if applied else "pending"
        path = persist_receipt(self.state, {**event, "scope": scope})
        payload = {"id": event["id"], "scope": scope, "summary": event["message"], "status": status,
                   "applied_entities": sorted(set(applied)), "pending_entities": staged,
                   "receipt": str(path), "stage": "queued", "visibility": "unknown"}
        if event.get("retry"):
            payload["retry"] = event["retry"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO notices VALUES (?,?,?,?)", (event["id"], scope, dumps(payload), time.time()))
            self.db.execute("INSERT OR IGNORE INTO notice_order(id) VALUES (?)", (event["id"],))

    def sync(self, consumer: str, *, after: int = 0, acknowledgements=None, limit: int = 20) -> dict:
        if type(after) is not int or after < 0:
            raise ValueError("Notification cursor must be a nonnegative integer")
        acknowledgements = acknowledgements or []
        if not isinstance(acknowledgements, list) or len(acknowledgements) > 100:
            raise ValueError("At most 100 acknowledgements are accepted")
        # Validate the entire batch before any acknowledgement takes effect.
        for ack in acknowledgements:
            if not isinstance(ack, dict) or ack.get("stage") not in {"sent", "read"}:
                raise ValueError("Invalid notification acknowledgement")
            if not self.db.execute("SELECT 1 FROM notices WHERE id=? AND scope=?", (ack.get("id"), ack.get("scope"))).fetchone():
                raise PermissionError("Unknown notice scope")
        for ack in acknowledgements:
            self.acknowledge(consumer, ack["id"], ack["scope"], ack["stage"])
        rows = self.db.execute("SELECT o.seq,n.id,n.scope,n.payload FROM notice_order o JOIN notices n ON n.id=o.id "
                               "WHERE o.seq>? ORDER BY o.seq LIMIT ?", (after, max(1, min(int(limit), 100)))).fetchall()
        items = []
        for row in rows:
            sent = self.db.execute("SELECT 1 FROM deliveries WHERE consumer=? AND id=? AND stage IN ('sent','read')",
                                   (consumer, row["id"])).fetchone()
            if not sent:
                payload = json.loads(row["payload"])
                if payload.get("retired"):
                    continue
                items.append({**{key: val for key, val in payload.items() if key != "receipt"},
                              "seq": row["seq"], "text": self.detail(row["id"], row["scope"])["text"]})
        return {"protocol_version": 2, "items": items, "cursor": rows[-1]["seq"] if rows else after}

    def poll(self, consumer: str, scope: str | None = None, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT n.payload FROM notices n WHERE (? IS NULL OR n.scope=?) "
                               "AND COALESCE(json_extract(n.payload,'$.retired'),0)=0 "
                               "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.consumer=? AND d.id=n.id "
                               "AND d.stage IN ('sent','read')) ORDER BY created,id LIMIT ?",
                               (scope, scope, consumer, max(1, min(limit, 100)))).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != "receipt"} for row in rows]

    def detail(self, identifier: str, scope: str) -> dict:
        row = self.db.execute("SELECT payload FROM notices WHERE id=? AND scope=?", (identifier, scope)).fetchone()
        if not row:
            raise PermissionError("Notice is unavailable in this workspace")
        value = json.loads(row[0])
        if value.get("retired"):
            return {"scope":scope,"summary":"Expired learning receipt","text":"Receipt detail expired under the 90-day retention policy."}
        path = Path(value["receipt"])
        if not path.resolve().is_relative_to((self.state / "receipts").resolve()):
            raise ValueError("Receipt path escapes the queue")
        return json.loads(path.read_text())

    def retire(self, *, before: float, dry_run=True):
        rows = self.db.execute("SELECT id,payload FROM notices n WHERE created<? "
                               "AND COALESCE(json_extract(payload,'$.retired'),0)=0 "
                               "AND EXISTS(SELECT 1 FROM deliveries d WHERE d.id=n.id AND d.stage IN ('sent','read')) LIMIT 1000",
                               (before,)).fetchall()
        if not dry_run:
            for row in rows:
                value = json.loads(row["payload"])
                path = Path(value["receipt"])
                if not path.is_symlink() and path.resolve().is_relative_to((self.state/"receipts").resolve()):
                    path.unlink(missing_ok=True)
                with self.db:
                    self.db.execute("UPDATE notices SET payload=? WHERE id=?",
                                    (dumps({"id":row["id"],"scope":value["scope"],"retired":True}),row["id"]))
        return {"eligible":len(rows),"retired":0 if dry_run else len(rows)}

    def acknowledge(self, consumer: str, identifier: str, scope: str, stage: str) -> dict:
        if stage not in {"sent", "read"}:
            raise ValueError("Notification acknowledgements are sent or read")
        if not self.db.execute("SELECT 1 FROM notices WHERE id=? AND scope=?", (identifier, scope)).fetchone():
            raise PermissionError("Unknown notice scope")
        with self.db:
            for value in (["sent", "read"] if stage == "read" else [stage]):
                self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)", (consumer, identifier, value, time.time()))
        return {"id": identifier, "stage": stage, "visibility": "unknown" if stage == "sent" else "opened"}


def publish(settings, event: dict):
    queue = NoticeQueue(settings.state)
    try:
        queue.publish(event)
    finally:
        queue.close()
