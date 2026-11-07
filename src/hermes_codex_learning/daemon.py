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
from .delegation import exclude_internal, register_delegation, task_input
from .knowledge import context_view, reconcile
from .normalize import merge_event, observation
from .notify import macos_notification, message, should_notify
from .review_receipt import ReviewWrites, receipt_text
from .store import Store, atomic_json, digest, dumps


class Bridge:
    def __init__(self, settings: Settings, api, review_writes: ReviewWrites | None = None):
        self.settings, self.api = settings, api
        self.review_writes = review_writes or ReviewWrites()
        self.store = Store(settings.state)
        self.stop = threading.Event()
        self.store.set("state", "starting")

    def emit(self, event: dict) -> None:
        event = {**event, "message": message(event)}
        self.store.notice(event)

    def handle_for(self, source: dict) -> str:
        result = self.api.open({**{k: source[k] for k in ("host", "project", "chat")}, "executor": "Codex App"})
        self.store.set("scope:" + result["handle"], {k: source[k] for k in ("host", "project", "chat")})
        return result["handle"]

    def ingest(self) -> None:
        for row in self.store.pending():
            payload = json.loads(row["payload"])
            excluded = exclude_internal(self.store, payload)
            if not excluded:
                register_delegation(self.store, row["id"], payload, settings=self.settings)
            data = None if excluded else merge_event(self.store, row)
            if data:
                self.observe_turn(data, row["id"])
            if not excluded and payload.get("hook_event_name") in {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"}:
                missing = [key for key in ("session_id", "turn_id") if not payload.get(key)]
                if missing:
                    gaps = self.store.get("coverage_gaps", [])
                    self.store.set("coverage_gaps", (gaps + [{"id": row["id"], "event": payload["hook_event_name"], "missing": missing}])[-100:])
            if not excluded and payload.get("hook_event_name") == "SessionEnd" and payload.get("session_id"):
                source = {"host": payload["bridge_host"], "project": payload["bridge_project"], "chat": payload["session_id"]}
                closing = self.store.get("closing", {})
                closing[row["id"]] = self.handle_for(source)
                self.store.set("closing", closing)
            if not excluded and payload.get("hook_event_name") == "PostToolUse" and "hermes_learning" in payload.get("tool_name", ""):
                self.usage(payload)
            with self.store.db:
                self.store.db.execute("UPDATE events SET delivered=?,error=NULL WHERE id=?", (time.time(), row["id"]))

    def observe_turn(self, data: dict, event_id: str) -> None:
        if data["status"] not in {"completed", "interrupted"}:
            return
        enriched = task_input(self.store, data, settings=self.settings)
        if not enriched["user"]:
            return
        handle = self.handle_for(enriched["source"])
        self.api.observe(handle, observation(enriched), event_id=event_id)
        revision = digest(enriched)
        key = digest([enriched["source"]["chat"], enriched["source"]["turn"]])
        with self.store.db:
            self.store.db.execute("UPDATE turns SET payload=?,revision=?,observed_revision=? WHERE id=?",
                                  (dumps(enriched), revision, revision, key))

    def recover_context(self) -> None:
        # Reuse original hooks and event ids after an upgrade or a late parent receipt.
        # No transcript import and no synthetic UserPromptSubmit event are involved.
        rows = self.store.db.execute("SELECT id,payload FROM events WHERE "
                                     "json_extract(payload,'$.tool_name') IN ('mcp__codex_app__create_thread','mcp__codex_app__read_thread') OR "
                                     "json_extract(payload,'$.hook_event_name')='UserPromptSubmit'").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if not exclude_internal(self.store, payload):
                register_delegation(self.store, row["id"], payload, settings=self.settings)
        for row in self.store.db.execute("SELECT payload FROM turns WHERE observed_revision IS NULL "
                                         "AND json_extract(payload,'$.user')='' "
                                         "AND json_extract(payload,'$.status') IN ('completed','interrupted')").fetchall():
            data = json.loads(row["payload"])
            if self.store.get("excluded-chat:" + data["source"]["chat"]):
                continue
            if not task_input(self.store, data, settings=self.settings)["user"]:
                continue
            event = self.store.db.execute("SELECT id FROM events WHERE json_extract(payload,'$.session_id')=? "
                                          "AND json_extract(payload,'$.turn_id')=? "
                                          "AND json_extract(payload,'$.hook_event_name') IN ('Stop','Interrupt') "
                                          "ORDER BY created DESC LIMIT 1", (data["source"]["chat"], data["source"]["turn"])).fetchone()
            if event:
                self.observe_turn(data, event["id"])

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
                self.emit({"id": "loaded:" + token, "kind": "skill.loaded", "name": delivered.get("name", ""),
                           "project": payload["bridge_project"], "chat": payload["session_id"]})

    def reviews(self) -> None:
        status = self.api.read(kind="status")
        for intent in status["intents"]:
            if intent["status"] != "queued":
                continue
            attempt_id = self.store.get("attempt:" + intent["id"])
            if not attempt_id:
                attempt_id = str(uuid.uuid4())
                self.store.set("attempt:" + intent["id"], attempt_id)
            source = self.store.get("scope:" + intent["handle"], {})
            provenance = {"project": source.get("project"), "source_chat": source.get("chat")}
            if self.settings.capture_scope == "global":
                self.update_knowledge(self.api.read(kind="context"))
            started_at = time.time()
            self.emit({"id": attempt_id + ":start", "kind": "review.started", **provenance})
            self.store.set("state", "reviewing")
            with self.review_writes.capture(intent["handle"]) as calls:
                result = self.api.run_review(intent["handle"], intent["id"], attempt_id=attempt_id)
            finished_at = time.time()
            if self.settings.capture_scope == "global":
                self.update_knowledge(self.api.read(kind="context"), project=source.get("project"), result=result)
            if result["run_status"] not in {"deferred", "superseded"}:
                event = {**result, "id": attempt_id + ":result", "kind": "review.finished", **provenance}
                if should_notify(event):
                    event["receipt"] = receipt_text({**event, "message": message(event)}, calls, finished_at,
                                                    started_at=started_at)
                self.emit(event)
            self.store.set("state", "idle")

    def idle_reviews(self) -> None:
        now = time.time()
        last = self.store.get("last_idle_request", 0)
        if now - last < self.settings.idle_interval:
            return
        # A finished chat can learn while another Codex chat is active. Curator below
        # still requires the whole project to be idle; the review worker remains serial.
        turns = [row for row in self.latest_turns() if json.loads(row["payload"])["status"] == "completed"
                 and json.loads(row["payload"])["user"]
                 and now - row["activity"] >= self.settings.idle_seconds]
        if not turns:
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
            if self.store.get("excluded-chat:" + data["source"]["chat"]):
                continue
            latest[data["source"]["chat"]] = row
        return list(latest.values())

    def update_knowledge(self, context: dict, **kwargs) -> dict:
        stored_index = self.store.get("knowledge-index-v1", {})
        index = stored_index
        if not index:
            # Migrate the previously single-project library once, without rewriting native files.
            index = reconcile({}, context, bootstrap_project=str(self.settings.project.resolve()))
            for attempt in self.api.read(kind="status").get("attempts", []):
                index = reconcile(index, context, project=str(self.settings.project.resolve()), result=attempt)
        updated = reconcile(index, context, bootstrap_project=str(self.settings.project.resolve()), **kwargs)
        if updated != stored_index:
            self.store.set("knowledge-index-v1", updated)
        return {**context, "bridge_provenance": updated}

    def publish(self) -> None:
        context = self.api.read(kind="context")
        if self.settings.capture_scope == "global":
            context = self.update_knowledge(context)
        atomic_json(self.settings.state / "context.json", context)
        atomic_json(self.settings.state / "status.json", {
            **self.api.read(kind="status"), "daemon": self.store.get("state"), "updated_at": time.time(),
        })

    def notifications(self) -> None:
        if not self.settings.notifications:
            return
        for event in self.store.consume("os-notification", 20):
            if should_notify(event):
                result = macos_notification(self.settings.state, event)
                self.store.set("notification:" + event["id"], result)

    def loop(self) -> None:
        self.store.set("state", "idle")
        delay = 1
        while not self.stop.is_set():
            try:
                self.ingest()
                self.recover_context()
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
                        "capture_scope": self.settings.capture_scope,
                        "knowledge_policy": "personal_library_scoped_memory_shared_skills",
                        "excluded_projects": list(self.settings.excluded_projects),
                        "projects": sorted({json.loads(row[0])["project"] for row in store.db.execute(
                            "SELECT value FROM meta WHERE key LIKE 'scope:%'")}),
                        "capture_coverage_gaps": store.get("coverage_gaps", []),
                        "capture_exclusions": [{"chat": row["key"][len("excluded-chat:"):], **json.loads(row["value"])}
                                               for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'excluded-chat:%'")]}
        if method == "context":
            path = self.settings.state / "context.json"
            context = json.loads(path.read_text()) if path.exists() else {"available": False}
            if self.settings.capture_scope == "global":
                return context_view(context, self.request_project(params), params.get("query", ""))
            return context
        if method == "skill_load":
            project = self.request_project(params)
            value = self.api.read(kind="skill", name=params["name"], file_path=params.get("file_path"))
            with StoreConnection(self.settings) as store:
                origin = store.get("knowledge-index-v1", {}).get("skills", {}).get(params["name"], {})
            return {**value, "request_project": project, "source_projects": origin.get("projects", []),
                    "applicability": "Shared procedure; project-specific examples and paths must be checked against the current task."}
        raise ValueError("Unknown bridge method")

    def request_project(self, params: dict) -> str | None:
        if "cwd" not in params:
            # Old MCP clients may still list shared skills, but never receive project memory by guessing cwd.
            return str(self.settings.project) if self.settings.capture_scope == "project" else None
        project = self.settings.project_for(params["cwd"])
        if project is None:
            raise ValueError("cwd is invalid or excluded from learning")
        return project


class StoreConnection:
    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        self.store = Store(self.settings.state)
        return self.store

    def __exit__(self, *args):
        self.store.close()


def run(settings: Settings, api, review_writes: ReviewWrites | None = None) -> int:
    os.umask(0o077)
    settings.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = (settings.state / "owner.lock").open("a+")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    owner.seek(0)
    owner.truncate()
    owner.write(str(os.getpid()))
    owner.flush()
    bridge = Bridge(settings, api, review_writes)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(1024 * 1024))
                result = {"result": bridge.rpc(request["method"], request.get("params", {}))}
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                self.wfile.write((dumps(result) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # Fire-and-forget wake clients close after their durable event write.

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
