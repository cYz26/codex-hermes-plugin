from __future__ import annotations

import fcntl
import json
import os
import signal
import socketserver
import threading
import time
import uuid

from .config import Settings
from .normalize import merge_event, observation
from .notify import macos_notification, message
from .store import Store, atomic_json, dumps


class Bridge:
    def __init__(self, settings: Settings, api):
        self.settings, self.api = settings, api
        self.store = Store(settings.state)
        self.stop = threading.Event()
        self.store.set("state", "starting")

    def emit(self, event: dict) -> None:
        event = {**event, "message": message(event)}
        self.store.notice(event)

    def handle_for(self, source: dict) -> str:
        result = self.api.open({**{k: source[k] for k in ("host", "project", "chat")}, "executor": "Codex App"})
        return result["handle"]

    def ingest(self) -> None:
        for row in self.store.pending():
            data = merge_event(self.store, row)
            if data and data["status"] in {"completed", "interrupted"} and data["user"]:
                handle = self.handle_for(data["source"])
                self.api.observe(handle, observation(data), event_id=row["id"])
            payload = json.loads(row["payload"])
            if payload.get("hook_event_name") in {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"}:
                missing = [key for key in ("session_id", "turn_id") if not payload.get(key)]
                if missing:
                    gaps = self.store.get("coverage_gaps", [])
                    self.store.set("coverage_gaps", (gaps + [{"id": row["id"], "event": payload["hook_event_name"], "missing": missing}])[-100:])
            if payload.get("hook_event_name") == "SessionEnd" and payload.get("session_id"):
                source = {"host": payload["bridge_host"], "project": payload["bridge_project"], "chat": payload["session_id"]}
                closing = self.store.get("closing", {})
                closing[row["id"]] = self.handle_for(source)
                self.store.set("closing", closing)
            if payload.get("hook_event_name") == "PostToolUse" and "hermes_learning" in payload.get("tool_name", ""):
                self.usage(payload)
            with self.store.db:
                self.store.db.execute("UPDATE events SET delivered=?,error=NULL WHERE id=?", (time.time(), row["id"]))

    def usage(self, payload: dict) -> None:
        if not payload.get("tool_name", "").endswith("skill_load"):
            return
        result = payload.get("tool_response", {})
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return
        if not isinstance(result, dict) or result.get("isError"):
            return
        delivered = result.get("structuredContent", {})
        if not delivered:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        delivered = json.loads(item["text"])
                    except ValueError:
                        continue
                    if isinstance(delivered, dict) and delivered.get("delivery_token"):
                        break
        token = delivered.get("delivery_token") if isinstance(delivered, dict) else None
        if token and payload.get("tool_use_id"):
            result = self.api.record_usage(token, {
                "source_event": [payload["session_id"], payload.get("turn_id"), payload["tool_use_id"]],
                "stage": "body_delivered",
            })
            if result.get("recorded"):
                self.emit({"id": "loaded:" + token, "kind": "skill.loaded", "name": delivered.get("name", "")})

    def reviews(self) -> None:
        status = self.api.read(kind="status")
        for intent in status["intents"]:
            if intent["status"] != "queued":
                continue
            attempt_id = self.store.get("attempt:" + intent["id"])
            if not attempt_id:
                attempt_id = str(uuid.uuid4())
                self.store.set("attempt:" + intent["id"], attempt_id)
            self.emit({"id": attempt_id + ":start", "kind": "review.started"})
            self.store.set("state", "reviewing")
            result = self.api.run_review(intent["handle"], intent["id"], attempt_id=attempt_id)
            if result["run_status"] not in {"deferred", "superseded"}:
                self.emit({**result, "id": attempt_id + ":result", "kind": "review.finished"})
            self.store.set("state", "idle")

    def idle_reviews(self) -> None:
        now = time.time()
        last = self.store.get("last_idle_request", 0)
        if now - last < self.settings.idle_interval:
            return
        # Every known chat must have a terminal boundary, not simply a quiet stream.
        turns = self.latest_turns()
        if not turns or any(json.loads(row["payload"])["status"] in {"running", "incomplete"} for row in turns):
            return
        if now - max(row["activity"] for row in turns) < self.settings.idle_seconds:
            return
        for row in turns:
            data = json.loads(row["payload"])
            if data["status"] == "completed":
                self.api.maintenance(handle=self.handle_for(data["source"]), idle_flush=True)
        self.store.set("last_idle_request", now)

    def latest_turns(self):
        latest = {}
        for row in self.store.db.execute("SELECT payload,stopped,activity FROM turns ORDER BY activity"):
            data = json.loads(row["payload"])
            latest[data["source"]["chat"]] = row
        return list(latest.values())

    def publish(self) -> None:
        context = self.api.read(kind="context")
        atomic_json(self.settings.state / "context.json", context)
        atomic_json(self.settings.state / "status.json", {
            **self.api.read(kind="status"), "daemon": self.store.get("state"), "updated_at": time.time(),
        })

    def notifications(self) -> None:
        if not self.settings.notifications:
            return
        for event in self.store.consume("os-notification", 20):
            if event["kind"] != "review.finished":
                continue
            if not (event.get("applied") or event.get("staged") or event.get("run_status") != "completed"):
                continue
            try:
                result = macos_notification(event["message"])
            except (OSError, TimeoutError) as exc:
                result = {"dispatched": False, "error": str(exc)}
            self.store.set("notification:" + event["id"], result)

    def loop(self) -> None:
        self.store.set("state", "idle")
        delay = 1
        while not self.stop.is_set():
            try:
                self.ingest()
                self.idle_reviews()
                self.reviews()
                closing = self.store.get("closing", {})
                for event_id, handle in list(closing.items()):
                    if self.api.close(handle=handle, event_id=event_id)["status"] != "deferred":
                        del closing[event_id]
                self.store.set("closing", closing)
                if not any(json.loads(row["payload"])["status"] in {"running", "incomplete"} for row in self.latest_turns()):
                    self.api.maintenance()
                self.publish()
                self.notifications()
                self.store.set("last_error", None)
                delay = 1
            except Exception as exc:
                self.store.set("last_error", f"{type(exc).__name__}: {exc}")
                self.store.set("state", "deferred")
                delay = min(delay * 2, 30)
            self.stop.wait(delay)

    def rpc(self, method: str, params: dict) -> dict:
        if method == "wake":
            return {"accepted": True}
        if method == "shutdown":
            self.stop.set()
            self.api.cancel()
            return {"accepted": True}
        if method == "learning_status":
            status = self.settings.state / "status.json"
            with StoreConnection(self.settings) as store:
                return {"snapshot": json.loads(status.read_text()) if status.exists() else None,
                        "state": store.get("state"), "error": store.get("last_error"),
                        "capture_coverage_gaps": store.get("coverage_gaps", [])}
        if method == "context":
            path = self.settings.state / "context.json"
            return json.loads(path.read_text()) if path.exists() else {"available": False}
        if method == "skill_load":
            return self.api.read(kind="skill", name=params["name"], file_path=params.get("file_path"))
        raise ValueError("Unknown bridge method")


class StoreConnection:
    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        self.store = Store(self.settings.state)
        return self.store

    def __exit__(self, *args):
        self.store.close()


def run(settings: Settings, api) -> int:
    os.umask(0o077)
    settings.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = (settings.state / "owner.lock").open("a+")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    owner.seek(0)
    owner.truncate()
    owner.write(str(os.getpid()))
    owner.flush()
    bridge = Bridge(settings, api)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(1024 * 1024))
                result = {"result": bridge.rpc(request["method"], request.get("params", {}))}
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((dumps(result) + "\n").encode())

    settings.socket.unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(str(settings.socket), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: (bridge.stop.set(), api.cancel()))
    try:
        bridge.loop()
    finally:
        server.shutdown()
        server.server_close()
        settings.socket.unlink(missing_ok=True)
        api.close()
        bridge.store.close()
        owner.close()
    return 0
