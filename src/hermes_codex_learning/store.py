from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.search(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization|secret)$|^token$|^private_key$", k)
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)Bearer\s+[\w.\-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\b(?:sk-[a-zA-Z0-9_-]{12,}|gh[pousr]_[a-zA-Z0-9_]{16,})", "[REDACTED]", value)
        value = re.sub(r"(?im)((?:[\w]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET))[\w]*\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value


class Store:
    """Peripheral evidence/outbox. Never writes a Hermes-owned database."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "bridge.sqlite", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, source_key TEXT UNIQUE, payload TEXT NOT NULL,
              created REAL NOT NULL, delivered REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS turns (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision TEXT NOT NULL,
              observed_revision TEXT, stopped REAL, activity REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notices (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
              payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
              consumer TEXT NOT NULL, seq INTEGER NOT NULL,
              PRIMARY KEY(consumer,seq));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('host_id',?)", (dumps(str(uuid.uuid4())),))

    def close(self) -> None:
        self.db.close()

    def capture(self, payload: dict) -> str:
        payload = redact(payload)
        event = payload.get("hook_event_name", "")
        session, turn = payload.get("session_id"), payload.get("turn_id")
        tool_id = payload.get("tool_use_id")
        source = None
        if session and turn and (tool_id or event in {"UserPromptSubmit", "Stop", "Interrupt"}):
            # Same identity + different content is a revision, not silently discarded.
            source = digest([session, turn, event, tool_id, digest(payload)])
        event_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,NULL,NULL)",
                            (event_id, source, dumps(payload), time.time()))
            if source:
                event_id = self.db.execute("SELECT id FROM events WHERE source_key=?", (source,)).fetchone()[0]
        return event_id

    def pending(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE delivered IS NULL ORDER BY created,id LIMIT 100")]

    def notice(self, event: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO notices(id,payload,created) VALUES (?,?,?)",
                            (event["id"], dumps(event), time.time()))

    def consume(self, consumer: str, limit: int = 3, *, project: str | None = None,
                chat: str | None = None, legacy_project: str | None = None) -> list[dict]:
        # Claim before returning: process crashes may lose a display, never replay it forever.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            scope_sql, scope_args = "", []
            if project is not None:
                scope_sql = (" AND (json_extract(n.payload,'$.project')=? OR "
                             "(json_extract(n.payload,'$.project') IS NULL AND ?=?)) "
                             "AND (json_extract(n.payload,'$.chat') IS NULL OR json_extract(n.payload,'$.chat')=?)")
                scope_args = [project, project, legacy_project, chat]
            rows = self.db.execute("SELECT seq,payload FROM notices n WHERE NOT EXISTS "
                                   "(SELECT 1 FROM deliveries d WHERE d.consumer=? AND d.seq=n.seq)" + scope_sql + " ORDER BY seq LIMIT ?",
                                   [consumer, *scope_args, limit]).fetchall()
            for row in rows:
                self.db.execute("INSERT INTO deliveries VALUES (?,?)", (consumer, row["seq"]))
        return [json.loads(r["payload"]) for r in rows]

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dumps(value)))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
