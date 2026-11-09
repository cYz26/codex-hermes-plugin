#!/usr/bin/env python3
"""Receive UI receipts through SSH. No Codex/MCP/model integration is used."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Receiver:
    def __init__(self, config):
        self.config = config
        self.state = Path(config["state"]).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state / "delivery.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, scope TEXT, ids TEXT, path TEXT, state TEXT)")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", config["host"]):
            raise ValueError("Use an existing SSH host alias")
        self.consumer = "desktop:" + identity([config["host"], str(self.state)])[:24]

    def remote(self, action, *args):
        command = shlex.join(["/usr/bin/python3", self.config["bridge"], "notices", action,
                              "--settings", self.config["settings"], *args])
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                 self.config["host"], command], capture_output=True, text=True, timeout=30, check=True)
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

    def poll(self):
        self.acknowledge()
        items = self.remote("poll", "--consumer", self.consumer)
        groups = {}
        known = {item for row in self.db.execute("SELECT ids FROM batches") for item in json.loads(row[0])}
        for item in items:
            if item["id"] not in known:
                groups.setdefault(item["scope"], []).append(item)
        for scope, group in groups.items():
            ids = sorted(item["id"] for item in group)
            identifier = identity([scope, ids])
            receipts = [self.remote("detail", "--id", item["id"], "--scope", scope) for item in group]
            applied = set().union(*(set(item["applied_entities"]) for item in group))
            pending = set().union(*(set(item["pending_entities"]) for item in group))
            summary = group[0]["summary"] if len(group) == 1 else (
                f"[{Path(scope).name}] {len(applied)} 项经验生效，{len(pending)} 项待处理；{len(group)} 条回执")
            path = self.state / (identifier + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"summary": summary, "scope": scope,
                                 "text": "\n\n".join(item["text"] for item in receipts)}, ensure_ascii=False))
            temporary.chmod(0o600)
            temporary.replace(path)
            with self.db:
                self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?)", (identifier, scope, json.dumps(ids), str(path), "dispatching"))
            result = subprocess.run([self.config["notifier"], "--post", str(path)], capture_output=True, timeout=15)
            if result.returncode:
                # A definite rejection is retryable; an interrupted dispatch is
                # retained as unknown instead of blindly posting duplicates.
                with self.db:
                    self.db.execute("DELETE FROM batches WHERE id=?", (identifier,))
                raise RuntimeError("macOS rejected the notification; check notification permission")
        self.acknowledge()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    receiver = Receiver(json.loads(args.config.read_text()))
    while True:
        try:
            receiver.poll()
            unknown = sum(1 for raw_path, state in receiver.db.execute("SELECT path,state FROM batches")
                          if state == "dispatching" and not Path(raw_path + ".sent").exists())
            status = {"state": "connected", "at": time.time(), "visibility": "not_inferred",
                      "unknown_dispatches": unknown, "chat_ui": "unavailable"}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            status = {"state": "retrying", "at": time.time(), "error_type": type(exc).__name__, "chat_ui": "unavailable"}
        (receiver.state / "status.json").write_text(json.dumps(status))
        if args.once:
            return 0 if status["state"] == "connected" else 1
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
