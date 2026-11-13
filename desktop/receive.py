#!/usr/bin/env python3
"""Receive UI receipts through SSH. No Codex/MCP/model integration is used."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path


APPROVAL_REQUIRED = "approval.required"


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def notifier_binary(configured: str) -> str:
    binary = Path(configured)
    if binary.is_file() or len(binary.parents) < 3:
        return configured
    app = binary.parents[2]
    if app.suffix != ".app" or app.exists():
        return configured
    legacy = app.parent / ".hermes-learning-legacy" / app.name
    try:
        with (legacy / "Contents/Info.plist").open("rb") as info:
            if plistlib.load(info).get("CFBundleIdentifier") == "com.cy.hermes-learning-notifier":
                return str(legacy / "Contents/MacOS" / binary.name)
    except FileNotFoundError:
        pass
    return configured


class Receiver:
    def __init__(self, config):
        self.config = config
        self.state = Path(config["state"]).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state / "delivery.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, scope TEXT, ids TEXT, path TEXT, state TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS received(id TEXT PRIMARY KEY, scope TEXT, payload TEXT, batch TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receiver_meta(key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS batch_times(id TEXT PRIMARY KEY, created REAL)")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            with self.db:
                for identifier,scope,ids,path,_ in self.db.execute("SELECT * FROM batches").fetchall():
                    for item in json.loads(ids):
                        self.db.execute("INSERT OR IGNORE INTO received VALUES (?,?,?,?)", (item,scope,"{}",identifier))
                    self.db.execute("INSERT OR IGNORE INTO batch_times VALUES (?,?)",
                                    (identifier,Path(path).stat().st_mtime if Path(path).exists() else time.time()))
                self.db.execute("PRAGMA user_version=2")
        self.protocol = None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", config["host"]):
            raise ValueError("Use an existing SSH host alias")
        self.consumer = "desktop:" + identity([config["host"], str(self.state)])[:24]

    def remote(self, action, *args, body=None):
        command = shlex.join([self.config.get("remote_python", "/usr/bin/python3"), self.config["bridge"], "notices", action,
                              "--settings", self.config["settings"], *args])
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                 self.config["host"], command], input=json.dumps(body) if body is not None else None,
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def acknowledge(self):
        for identifier, scope, raw_ids, raw_path, state in self.db.execute("SELECT * FROM batches").fetchall():
            path = Path(raw_path)
            opened = Path(raw_path + ".read").exists()
            accepted = Path(raw_path + ".sent").exists()
            target = "read" if opened else "sent" if accepted else None
            if not target or state == "read" or state == target:
                continue
            for item in json.loads(raw_ids):
                self.remote("ack", "--consumer", self.consumer, "--id", item, "--scope", scope, "--stage", target)
            with self.db:
                self.db.execute("UPDATE batches SET state=? WHERE id=?", (target, identifier))

    def write_batch(self, scope, group, text):
        ids = sorted(item["id"] for item in group)
        identifier = identity([scope, ids])
        receipt = {"summary": group[0]["summary"], "scope": scope, "text": text}
        approval = group[0].get("kind") == APPROVAL_REQUIRED
        if len(group) > 1:
            pending = {proposal for item in group for proposal in item["pending_entities"]}
            project = Path(scope).name or scope
            if approval:
                receipt["summary"] = f"[{project}] {len(pending)} 项提案待审批"
            else:
                applied = {entity for item in group for entity in item["applied_entities"]}
                receipt["summary"] = (f"[{project}] {len(applied)} 项经验生效，"
                                      f"{len(pending)} 项待处理；{len(group)} 条回执")
        if approval:
            receipt.update(kind=APPROVAL_REQUIRED,
                           project_approval=any(item.get("project_approval") is True for item in group))
        path = self.state / (identifier + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(path)
        return identifier, ids, path

    def poll(self):
        if self.protocol is None:
            self.protocol = self.remote("capabilities").get("protocol_version", 1)
        if self.protocol >= 2:
            return self.poll_v2()
        self.acknowledge()
        items = self.remote("poll", "--consumer", self.consumer)
        groups = {}
        known = {item for row in self.db.execute("SELECT ids FROM batches") for item in json.loads(row[0])}
        for item in items:
            if item["id"] not in known:
                groups.setdefault((item["scope"], item.get("kind") == APPROVAL_REQUIRED), []).append(item)
        for (scope, _), group in groups.items():
            receipts = [self.remote("detail", "--id", item["id"], "--scope", scope) for item in group]
            identifier, ids, path = self.write_batch(scope, group, "\n\n".join(item["text"] for item in receipts))
            with self.db:
                self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?)", (identifier, scope, json.dumps(ids), str(path), "dispatching"))
            result = subprocess.run([notifier_binary(self.config["notifier"]), "--post", str(path)], capture_output=True, timeout=15)
            if result.returncode:
                # A definite rejection is retryable; an interrupted dispatch is
                # retained as unknown instead of blindly posting duplicates.
                with self.db:
                    self.db.execute("DELETE FROM batches WHERE id=?", (identifier,))
                raise RuntimeError("macOS rejected the notification; check notification permission")
        self.acknowledge()

    def poll_v2(self):
        cursor = self.db.execute("SELECT value FROM receiver_meta WHERE key='cursor'").fetchone()
        acks, changes = [], []
        for identifier, scope, raw_ids, raw_path, state in self.db.execute(
                "SELECT * FROM batches WHERE state NOT IN ('read','abandoned')").fetchall():
            target = "read" if Path(raw_path + ".read").exists() else "sent" if Path(raw_path + ".sent").exists() else None
            ids = json.loads(raw_ids)
            if target and target != state and len(acks) + len(ids) <= 100:
                acks.extend({"id": item, "scope": scope, "stage": target} for item in ids)
                changes.append((target, identifier))
        value = self.remote("sync", "--consumer", self.consumer,
                            body={"after": int(cursor[0]) if cursor else 0, "acks": acks})
        # All receipt bodies and the next cursor commit together, before dispatch.
        with self.db:
            self.db.executemany("UPDATE batches SET state=? WHERE id=?", changes)
            for item in value["items"]:
                self.db.execute("INSERT OR IGNORE INTO received VALUES (?,?,?,NULL)",
                                (item["id"], item["scope"], json.dumps(item)))
            self.db.execute("INSERT OR REPLACE INTO receiver_meta VALUES ('cursor',?)", (str(value["cursor"]),))
        groups = {}
        for raw, in self.db.execute("SELECT payload FROM received WHERE batch IS NULL ORDER BY rowid LIMIT 100"):
            item = json.loads(raw)
            groups.setdefault((item["scope"], item.get("kind") == APPROVAL_REQUIRED), []).append(item)
        for (scope, _), group in groups.items():
            identifier, ids, path = self.write_batch(scope, group, "\n\n".join(item["text"] for item in group))
            with self.db:
                self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?)",
                                (identifier, scope, json.dumps(ids), str(path), "dispatching"))
                self.db.execute("INSERT OR REPLACE INTO batch_times VALUES (?,?)", (identifier,time.time()))
                self.db.executemany("UPDATE received SET batch=? WHERE id=?", [(identifier, item) for item in ids])
            result = subprocess.run([self.config["notifier"], "--post", str(path)], capture_output=True, timeout=15)
            if result.returncode == 1:  # The bundled notifier reports an explicit rejection with exit 1.
                with self.db:
                    self.db.execute("UPDATE received SET batch=NULL WHERE batch=?", (identifier,))
                    self.db.execute("DELETE FROM batches WHERE id=?", (identifier,))
                raise RuntimeError("macOS rejected the notification")
            # Missing markers, signals and interrupted dispatches remain unknown.
        self.retire_details()

    def retire_details(self):
        row = self.db.execute("SELECT value FROM receiver_meta WHERE key='last_cleanup'").fetchone()
        if row and time.time()-float(row[0]) < 86400:
            return
        rows = self.db.execute("SELECT b.id,b.path FROM batches b JOIN batch_times t ON t.id=b.id "
                               "WHERE b.state IN ('sent','read','abandoned') AND t.created<? LIMIT 100",
                               (time.time()-90*86400,)).fetchall()
        for identifier,raw_path in rows:
            path = Path(raw_path)
            if path.parent.resolve() != self.state.resolve():
                continue
            for suffix in ("",".sent",".read"):
                Path(raw_path+suffix).unlink(missing_ok=True)
            with self.db:
                self.db.execute("UPDATE batches SET state='retired' WHERE id=?", (identifier,))
                self.db.execute("UPDATE received SET payload='{}' WHERE batch=?", (identifier,))
        if len(rows) < 100:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO receiver_meta VALUES ('last_cleanup',?)",(str(time.time()),))

    def resolve(self, identifier: str, resolution: str):
        row = self.db.execute("SELECT path,state FROM batches WHERE id=?", (identifier,)).fetchone()
        if not row or row[1] in {"sent", "read"}:
            raise ValueError("Only an unresolved batch may be resolved")
        with self.db:
            if resolution == "abandon":
                self.db.execute("UPDATE batches SET state='abandoned' WHERE id=?", (identifier,))
            elif resolution == "retry" and not Path(row[0] + ".sent").exists():
                self.db.execute("UPDATE received SET batch=NULL WHERE batch=?", (identifier,))
                self.db.execute("DELETE FROM batches WHERE id=?", (identifier,))
            else:
                raise ValueError("Retry requires an unresolved dispatch without a sent marker")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resolve", help="Batch ID whose dispatch has been independently checked")
    parser.add_argument("--resolution", choices=["retry", "abandon"])
    args = parser.parse_args()
    os.umask(0o077)
    receiver = Receiver(json.loads(args.config.read_text()))
    if args.resolve:
        receiver.resolve(args.resolve, args.resolution)
        return 0
    delay = 5
    while True:
        try:
            receiver.poll()
            unknown = sum(1 for raw_path, state in receiver.db.execute("SELECT path,state FROM batches")
                          if state == "dispatching" and not Path(raw_path + ".sent").exists())
            status = {"state": "connected", "at": time.time(), "visibility": "not_inferred",
                      "unknown_dispatches": unknown, "chat_ui": "unavailable"}
            delay = 5
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            status = {"state": "retrying", "at": time.time(), "error_type": type(exc).__name__, "chat_ui": "unavailable"}
            delay = min(delay * 2, 60)
        (receiver.state / "status.json").write_text(json.dumps(status))
        if args.once:
            return 0 if status["state"] == "connected" else 1
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
