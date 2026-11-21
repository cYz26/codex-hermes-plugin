from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sys
import socketserver
import threading
import time
import uuid
from pathlib import Path

from .config import Settings
from .delegation import exclude_internal, register_delegation, task_input
from .knowledge import context_view, memory_entries, reconcile
from .learning_health import native_policy, record_capacity_refusal, snapshot as learning_health
from .normalize import TERMINAL_EVENTS, merge_event, observation
from .notify import approval_notice, macos_notification, message, notification_events, should_notify
from .publication_guardian import PublicationGuardian
from .review_deadline import run_review
from .review_receipt import ReviewWrites, receipt_text
from .review_summary import capture_review_summary
from .review_retries import ReviewRetryQueue
from .store import Store, atomic_json, digest, dumps
from . import native_reads


class Bridge:
    def __init__(self, settings: Settings, api, review_writes: ReviewWrites | None = None):
        self.settings, self.api = settings, api
        self.review_writes = review_writes or ReviewWrites()
        self.store = Store(settings.state)
        self.retry_queue = ReviewRetryQueue(settings, self.store)
        from .read_receipts import initialize
        initialize(settings.state)
        self.stop = threading.Event()
        self.drain = threading.Event()
        self.ready = threading.Event()
        self.resume = threading.Event()
        self.publication_lock = threading.Lock()
        self.publication_guardian = PublicationGuardian()
        self.startup_context = None
        self.startup_status = None
        from .identity import build_identity
        self.build = build_identity()
        if self.store.get("input-retry-build") != self.build["digest"]:
            with self.store.db:
                self.store.db.execute("DELETE FROM meta WHERE key LIKE 'input-retry:%'")
            self.store.set("last_input_retry", 0)
            self.store.set("input-retry-build", self.build["digest"])
        self.last_publish = 0.0
        self.native_capabilities = {}
        self.native_policy = None
        if os.environ.get("HERMES_LEARNING_START_PAUSED") != "1":
            self.resume.set()
        self.store.set("state", "starting")

    def emit(self, event: dict, *, calls: dict | None = None) -> None:
        reminder = approval_notice(self.settings, event)
        if reminder:
            event = {**event, "approval_notice": reminder}
        event = {**event, "message": message(event)}
        if calls is not None and (reminder or should_notify(event)):
            event["receipt"] = receipt_text(event, calls, event["finished_at"], started_at=event["started_at"])
        from .notices import publish
        publish(self.settings, self.store.notice(event))

    def handle_for(self, source: dict) -> str:
        identity = {**{k: source[k] for k in ("host", "project", "chat")}, "executor": source.get("executor", "Codex App")}
        result = self.api.open(identity)
        self.store.set("scope:" + result["handle"], identity)
        self.bind_chat_handle(source, result["handle"])
        return result["handle"]

    def ingest(self) -> None:
        from .delivery import drain_deliveries
        drain_deliveries(self.store, self.usage)
        self.store.set("loop-progress",{**self.store.get("loop-progress",{}),"phase":"consuming","updated_at":time.time()})
        started, consumed = time.monotonic(), 0
        while consumed < 1000 and time.monotonic()-started < 2:
            rows = self.store.pending()
            if not rows:
                break
            for row in rows:
                if not self.consume_event(row):
                    break
                consumed += 1
                if consumed >= 1000 or time.monotonic()-started >= 2:
                    break
            else:
                continue
            break
        from .feedback import reconcile as reconcile_feedback
        reconcile_feedback(self.store)

    def consume_event(self, row) -> bool:
        payload = json.loads(row["payload"])
        excluded = exclude_internal(self.store, payload)
        prepared = None
        if not excluded:
            register_delegation(self.store, row["id"], payload, settings=self.settings)
            if payload.get("session_id") and payload.get("turn_id"):
                key = digest([payload["session_id"], payload["turn_id"]])
                prepared = self.store.get("background-observation:" + key)
                if prepared:
                    self.observe_turn(prepared["turn"], prepared["event"])
                    if self.store.get("background-observation:" + key):
                        return False  # Keep the queued event until the previous observation is resolved.
        data = None if excluded or (prepared and prepared["event"] == row["id"]) else merge_event(self.store, row)
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
            handles = self.store.get("chat-handles:" + digest(source), [])
            if not handles:
                for existing in self.store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'scope:%'").fetchall():
                    handle = existing["key"][len("scope:"):]
                    local = self.store.get("workflow-local-source:" + handle, json.loads(existing["value"]))
                    if all(local.get(key) == value for key,value in source.items()):
                        handles.append(handle)
            # Do not create a new native scope merely to close a chat.
            for handle in handles:
                closing[row["id"] + ":" + handle] = handle
            self.store.set("closing", closing)
        with self.store.db:
            self.store.db.execute("UPDATE events SET delivered=?,error=NULL WHERE id=?", (time.time(), row["id"]))
        self.store.set("loop-progress",{"phase":"consuming","updated_at":time.time(),
            "last_event":row["id"],"chat":payload.get("session_id"),"turn":payload.get("turn_id"),"captured_at":row["created"]})
        return True

    def observe_turn(self, data: dict, event_id: str, *, retrying_input=False) -> None:
        key = digest([data["source"]["chat"],data["source"]["turn"]])
        if data.get("_skip_observation") == event_id:
            return
        if data.get('_evidence_mode') == 'workflow-v1':
            self.observe_workflow(data, event_id)
            return
        if self.store.get("withheld-turn:" + key):
            return
        pending_key = "background-observation:" + key
        prepared = self.store.get(pending_key)
        if prepared:
            if prepared["event"] != event_id or prepared["turn"]["source"] != data["source"]:
                return  # An unconfirmed write for this turn must be resolved first.
            enriched = prepared["turn"]
        else:
            previous = self.store.get("observation-receipt:" + key, {})
            if previous.get("background") and previous.get("event") == event_id:
                return
            native_host = data["source"].get("executor") == "Hermes"
            enriched = data if native_host else task_input(self.store, data, settings=self.settings)
            if not native_host and (not enriched.get("user") or data["status"] in {"completed", "interrupted", "incomplete"}):
                from .host_evidence import supplement
                enriched = supplement(self.store, enriched, settings=self.settings, event_id=event_id)
            states = [tool["background_session"] for tool in enriched["tools"].values() if tool.get("background_session")]
            if any(state.get("pending_exit_event") == event_id for state in states):
                return  # A Post without an actual exit must not add a native revision.
            if states and not previous.get("background") and not any(state.get("exit_event") == event_id for state in states):
                terminal = enriched.get("evidence_provenance", {}).get("terminal_event")
                if terminal:
                    event_id = terminal
        if enriched["source"].get("executor") != "Hermes" and enriched.get("_ended_by") in TERMINAL_EVENTS:
            from .delivery import capture_observed_skill_reads
            capture_observed_skill_reads(self.store, enriched, self.settings)
        from .scheduling import is_explicit_correction
        if (self.settings.proactive and enriched["status"] == "incomplete" and enriched.get("_ended_by") == "Stop"
                and is_explicit_correction(enriched.get("user"))):
            enriched={**enriched,"status":"completed","learning_basis":"dialogue_with_unresolved_execution",
                      "coverage":sorted(set(enriched.get("coverage",[])) | {"execution_results_unresolved"})}
        projected = observation(enriched)
        if self.settings.data_policy:
            from .policy import admit
            assessment = admit(projected, self.settings.data_policy, data["source"]["project"])
            if not assessment["allowed"]:
                self.store.set("turn-admission:"+key,{"state":"withheld","reason":assessment.get("reason"),"at":time.time()})
                return
            if self.settings.data_policy.get("mode") in {"trusted_enterprise", "trusted_local"}:
                from .enterprise import scrub
                enriched=scrub(enriched)
        else:
            assessment = {"allowed":True, "basis":"legacy_configuration"}
        if not enriched["user"]:
            self.store.set("turn-admission:"+key,{"state":"awaiting_input","reason":"request_body_unavailable","at":time.time()})
            return
        if enriched["status"] not in {"completed", "interrupted"}:
            self.store.set("turn-admission:"+key,{"state":"awaiting_terminal_evidence","at":time.time()})
            if enriched != data:
                key = digest([enriched["source"]["chat"], enriched["source"]["turn"]])
                with self.store.db:
                    self.store.db.execute("UPDATE turns SET payload=?,revision=? WHERE id=?", (dumps(enriched), digest(enriched), key))
            return
        if enriched.get("source", {}).get("executor") != "Hermes":
            from .model_sync import record_observed_codex_model
            record_observed_codex_model(self.store, enriched)
        handle = self.handle_for(enriched["source"])
        from .enterprise import is_trusted_local, scope_boundary
        if not is_trusted_local(self.settings.data_policy):
            required=scope_boundary(self.store,handle)
            if required and required!=assessment.get("boundary_id"):
                self.store.set("policy-withheld:"+event_id,"enterprise_scope_not_active")
                return
        if assessment.get("boundary_id"):
            self.store.set("enterprise-scope:"+handle,assessment["boundary_id"])
        self.bind_chat_handle(enriched["source"], handle)
        self.store.set("assessment:" + handle, {key:value for key,value in assessment.items() if key != "payload"})
        admitted = assessment.get("payload", projected)
        background = any(tool.get("background_session") for tool in enriched["tools"].values())
        if prepared or background:
            identity = digest([handle, admitted])
            if prepared and prepared["identity"] != identity:
                self.store.set("turn-admission:" + key, {"state":"withheld", "at":time.time(),
                               "reason":"prepared_observation_no_longer_matches_admission"})
                return
            if not prepared:
                # Freeze the admitted input before crossing the native boundary.
                # New transcript bytes must not change a possibly committed event.
                self.store.set(pending_key, {"event":event_id, "turn":enriched, "identity":identity})
        receipt = self.api.observe(handle, admitted, event_id=event_id)
        self.store.set("turn-admission:"+key,{"state":"admitted","at":time.time(),"basis":assessment.get("basis"),
                                            "learning_basis":enriched.get("learning_basis","terminal_execution")})
        if isinstance(receipt,dict):
            self.store.set("observation-receipt:"+key,{"handle":handle,"revision":receipt.get("revision"),"event":event_id,
                "at":time.time(), **({"background":True} if background else {})})
        recovered = retrying_input and data["status"] == "incomplete" and enriched["status"] == "completed" and all(
            tool["status"] in {"completed", "failed", "interrupted"} for tool in enriched["tools"].values())
        if self.settings.proactive or recovered:
            from .scheduling import enqueue
            enqueue(self.store, enriched, handle, recovered_terminal=recovered)
        revision = digest(enriched)
        key = digest([enriched["source"]["chat"], enriched["source"]["turn"]])
        with self.store.db:
            self.store.db.execute("UPDATE turns SET payload=?,revision=?,observed_revision=?,stopped=COALESCE(stopped,?) WHERE id=?",
                                  (dumps(enriched), revision, revision,
                                   enriched.get("evidence_provenance", {}).get("terminal_at") if background else None, key))
            if prepared or background:
                self.store.db.execute("DELETE FROM meta WHERE key=?", (pending_key,))
                if not any((tool.get("background_session") or {}).get("pending_exit_event")
                           for tool in enriched["tools"].values()):
                    self.store.db.execute("DELETE FROM meta WHERE key=?", ("input-retry:" + key,))

    def observe_workflow(self, data: dict, event_id: str) -> None:
        from .workflow import observation as workflow_observation
        if data.get('_ended_by') not in {'Stop', 'Interrupt'}:
            return
        if data["source"].get("executor") != "Hermes":
            from .host_evidence import supplement
            data = supplement(self.store, data, settings=self.settings, include_task=False)
        data['status'] = 'interrupted' if data['_ended_by'] == 'Interrupt' else 'completed'
        value = workflow_observation(data)
        handle = self.api.open({k: value['source'][k] for k in ('host', 'project', 'chat', 'executor')})['handle']
        self.store.set('scope:' + handle, {**value['source'], 'evidence_mode': 'workflow-v1'})
        self.store.set('workflow-local-source:' + handle, data['source'])
        self.bind_chat_handle(data['source'], handle)
        receipt = self.api.observe(handle, value, event_id=event_id)
        key = digest([data['source']['chat'], data['source']['turn']])
        self.store.set("turn-admission:"+key,{"state":"workflow_only","body_retained":False,"at":time.time()})
        if isinstance(receipt,dict):
            self.store.set("observation-receipt:"+key,{"handle":handle,"revision":receipt.get("revision"),"event":event_id,"at":time.time()})
        if self.settings.proactive:
            from .scheduling import enqueue
            enqueue(self.store, data, handle)
        key = digest([data['source']['chat'], data['source']['turn']])
        with self.store.db:
            self.store.db.execute('UPDATE turns SET payload=?,revision=?,observed_revision=? WHERE id=?',
                                  (dumps(data), digest(data), digest(data), key))

    def bind_chat_handle(self, source: dict, handle: str) -> None:
        identity = {key: source[key] for key in ("host", "project", "chat")}
        key = "chat-handles:" + digest(identity)
        self.store.set(key, sorted(set(self.store.get(key, [])) | {handle}))

    def recover_context(self) -> None:
        # Reuse original hooks and event ids after an upgrade or a late parent receipt.
        # No transcript import and no synthetic UserPromptSubmit event are involved.
        rows = self.store.db.execute("SELECT rowid,id,payload FROM events WHERE rowid>? ORDER BY rowid LIMIT 100",
                                     (self.store.get("recovery-cursor", 0),)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if not exclude_internal(self.store, payload):
                register_delegation(self.store, row["id"], payload, settings=self.settings)
            self.store.set("recovery-cursor", row["rowid"])
        for row in self.store.db.execute("SELECT payload FROM turns WHERE observed_revision IS NULL "
                                         "AND json_extract(payload,'$.user')='' "
                                         "AND json_extract(payload,'$.status') IN ('completed','interrupted')").fetchall():
            data = json.loads(row["payload"])
            if self.store.get("excluded-chat:" + data["source"]["chat"]):
                continue
            from .host_evidence import supplement
            if not supplement(self.store, task_input(self.store, data, settings=self.settings), settings=self.settings)["user"]:
                continue
            event = self.store.db.execute("SELECT id FROM events WHERE json_extract(payload,'$.session_id')=? "
                                          "AND json_extract(payload,'$.turn_id')=? "
                                          "AND json_extract(payload,'$.hook_event_name') IN ('Stop','Interrupt','CodexTranscriptComplete') "
                                          "ORDER BY created DESC LIMIT 1", (data["source"]["chat"], data["source"]["turn"])).fetchone()
            if event:
                self.observe_turn(data, event["id"])

    def retry_completed_inputs(self) -> None:
        now = time.time()
        if now - self.store.get("last_input_retry", 0) < 30:
            return
        self.store.set("last_input_retry", now)
        rows = self.store.db.execute("SELECT t.id,t.payload,t.observed_revision FROM turns t LEFT JOIN meta m ON m.key='input-retry:'||t.id "
                                     "WHERE json_extract(t.payload,'$.status') IN ('completed','interrupted','incomplete') "
                                     "AND (observed_revision IS NULL OR (json_extract(t.payload,'$.coverage') LIKE '%background_exit_pending%' AND EXISTS ("
                                     "SELECT 1 FROM json_each(t.payload,'$.tools') "
                                     "WHERE json_extract(value,'$.background_session.pending_exit_event') IS NOT NULL))) "
                                     "AND COALESCE(json_extract(m.value,'$.retry_at'),0)<=? "
                                     "ORDER BY COALESCE(json_extract(m.value,'$.last_attempt'),0),activity,t.id LIMIT 8",(now,)).fetchall()
        for row in rows:
            data = json.loads(row["payload"])
            prepared = self.store.get("background-observation:" + row["id"])
            event = self.store.db.execute("SELECT id FROM events WHERE json_extract(payload,'$.session_id')=? "
                                          "AND json_extract(payload,'$.turn_id')=? "
                                          "AND json_extract(payload,'$.hook_event_name') IN ('Stop','Interrupt','CodexTranscriptComplete') "
                                          "ORDER BY created DESC LIMIT 1", (data["source"]["chat"], data["source"]["turn"])).fetchone()
            if prepared:
                self.observe_turn(prepared["turn"], prepared["event"])
            elif row["observed_revision"] is not None:
                pending_events = {state["pending_exit_event"] for tool in data["tools"].values()
                                  if (state := tool.get("background_session")) and state.get("pending_exit_event")}
                for pending_event in sorted(pending_events)[:8]:
                    self.observe_turn(data, pending_event)
                    updated = self.store.db.execute("SELECT payload FROM turns WHERE id=?", (row["id"],)).fetchone()
                    data = json.loads(updated["payload"])
            elif event:
                self.observe_turn(data, event["id"], retrying_input=True)
            current = self.store.db.execute("SELECT observed_revision,payload FROM turns WHERE id=?",(row["id"],)).fetchone()
            current_tools = json.loads(current["payload"])["tools"].values()
            waiting_exit = any((tool.get("background_session") or {}).get("pending_exit_event") for tool in current_tools)
            observed = current["observed_revision"] is not None and not waiting_exit
            previous = self.store.get("input-retry:"+row["id"],{}) or {}
            attempts = previous.get("attempts",0)+1
            self.store.set("input-retry:"+row["id"],None if observed else {"last_attempt":now,"attempts":attempts,
                "retry_at":now+min(900,30*2**min(attempts-1,5)),"reason":"background_exit_evidence_pending" if waiting_exit else
                    self.store.get("turn-admission:"+row["id"],{}).get("reason","terminal_input_unavailable")})

    def usage(self, payload: dict) -> None:
        tool_name = payload.get("tool_name", "")
        if not (tool_name.endswith("skill_load") or tool_name == "native_skill_file_read"):
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
            from .read_receipts import PREFIX, verify
            if token.startswith(PREFIX):
                value=verify(self.settings.state,token,payload["bridge_project"])
                if value["name"]!=delivered.get("name") or value["version"]!=delivered.get("version"):
                    raise ValueError("Delivery receipt does not match the returned artifact")
                key="confirmed-read:"+digest(token)
                if self.store.get(key,{}).get("recorded"):
                    return
                native=self.store.get(key,{}).get("native_token")
                if not native:
                    receipt=native_reads.delivered_snapshot(self.api,value["name"],value["version"],value["file_path"])
                    native=receipt.get("delivery_token") if receipt else None
                    self.store.set(key,{"native_token":native})
                evidence={"stage":"body_delivered","source_event":[payload["session_id"],payload.get("turn_id"),payload["tool_use_id"]]}
                if native:
                    self.api.record_usage(native,evidence)
                delivery_meta = {key: delivered[key] for key in ("delivery_mode", "read_coverage") if delivered.get(key)}
                self.store.set("issued-delivery:"+token,{**value,**delivery_meta,"project":payload["bridge_project"],"source_event":evidence["source_event"]})
                self.store.set(key,{"recorded":True,"native_token":native})
                return
            if token.startswith("bridge:"):
                receipt = self.store.get("delivery:" + token)
                if receipt and not receipt.get("source_event") and receipt.get("project") == payload.get("bridge_project"):
                    self.store.set("delivery:" + token, {**receipt, "stage": "body_delivered",
                                   "source_event": [payload["session_id"], payload.get("turn_id"), payload["tool_use_id"]]})
                return
            result = self.api.record_usage(token, {
                "source_event": [payload["session_id"], payload.get("turn_id"), payload["tool_use_id"]],
                "stage": "body_delivered",
            })
            if result.get("recorded"):
                self.emit({"id": "loaded:" + token, "kind": "skill.loaded", "name": delivered.get("name", ""),
                           "project": payload["bridge_project"], "chat": payload["session_id"]})

    def reviews(self) -> None:
        from .model_sync import record_review_model_failure, sync_with_codex
        sync_with_codex(self)
        from .enterprise import MODE, boundary, is_trusted_local, scope_boundary
        trusted_local = is_trusted_local(self.settings.data_policy)
        enterprise=boundary(self.settings.data_policy)
        if self.settings.data_policy.get("mode")==MODE and not enterprise:
            self.store.set("enterprise-policy-state","boundary_changed")
            self.last_publish = 0.0
            return
        self.store.set("enterprise-policy-state","trusted_local" if trusted_local else ("ready" if enterprise else "not_enabled"))
        retry_available = callable(getattr(self.api, "retry_review", None))
        if retry_available and not self.drain.is_set():
            def local_source(handle, source):
                return self.store.get("workflow-local-source:" + handle, self.store.get("scope:" + handle, source))
            for retry in self.retry_queue.sync(self.api, source_for=local_source):
                if retry["state"] in {"waiting", "exhausted", "blocked"}:
                    self.emit({"id": "retry-scheduled:" + digest([retry["root_intent"], retry["retry_index"], retry["state"], retry["reason"]]),
                               "kind": "review.retry_scheduled", "project": retry.get("project"),
                               "source_chat": retry.get("chat"), "run_status": retry.get("outcome_status"),
                               "error": retry.get("error"), "retry": retry})
            self.retry_queue.process(self.api)
        status = native_reads.status(self.api)
        from .scheduling import ordered_intents
        from . import learning_dispatch
        intents = status["intents"] + learning_dispatch.recover_intents(self, status["intents"])
        self.store.set("review-queue",{"updated_at":time.time(),"intents":[{key:intent[key] for key in
                       ("id","handle","revision","status","reason") if key in intent} for intent in intents]})
        for intent in ordered_intents(self.store, intents):
            if self.drain.is_set():
                break
            if intent["status"] != "queued":
                continue
            if not trusted_local:
                required=scope_boundary(self.store,intent["handle"])
                if required and (not enterprise or required!=enterprise["id"]):
                    self.store.set("policy-withheld:"+intent["id"],"enterprise_boundary_not_active")
                    continue
            if not learning_dispatch.begin(self, intent):
                continue
            if intent.get("reason") in {"native_threshold", "idle_flush"} and type(intent.get("revision")) is int:
                reviewed = native_reads.reviewed_revision(self.api, intent["handle"])
                if reviewed is not None and intent["revision"] <= reviewed:
                    # Retire the old intent through the native owner's guard.
                    # It can close it without a model call or a started receipt.
                    self.api.run_review(intent["handle"], intent["id"], attempt_id=str(uuid.uuid4()))
                    continue
            attempt_id = self.store.get("attempt:" + intent["id"])
            if not attempt_id:
                attempt_id = str(uuid.uuid4())
                self.store.set("attempt:" + intent["id"], attempt_id)
            source = self.store.get("scope:" + intent["handle"], {})
            if enterprise and source.get("evidence_mode")!="workflow-v1":
                self.store.set("enterprise-scope:"+intent["handle"],enterprise["id"])
                assessment=self.store.get("assessment:"+intent["handle"],{})
                self.store.set("assessment:"+intent["handle"],{**assessment,"allowed":True,"classification":"approved",
                               "audience":"enterprise","boundary_id":enterprise["id"],"policy_version":enterprise["policy_version"]})
            elif trusted_local and source.get("evidence_mode")!="workflow-v1":
                assessment=self.store.get("assessment:"+intent["handle"],{})
                self.store.set("assessment:"+intent["handle"],{**assessment,"allowed":True,"classification":"approved",
                               "audience":"local","policy_version":"local-v1"})
            local = self.store.get("workflow-local-source:" + intent["handle"], source)
            provenance = {"project": local.get("project"), "source_chat": local.get("chat")}
            before_context = self.api.read(kind="context")
            before_versions = self.skill_versions(before_context)
            if self.settings.capture_scope == "global" or enterprise:
                self.update_knowledge(before_context)
            started_at = time.time()
            self.emit({"id": attempt_id + ":start", "kind": "review.started", **provenance,
                       "retry_index": intent.get("retry_index", 0)})
            self.store.set("state", "reviewing")
            self.store.set("loop-progress",{**self.store.get("loop-progress",{}),"phase":"reviewing","updated_at":started_at,"handle":intent["handle"],"intent":intent["id"]})
            self.store.set("review-progress:"+intent["handle"],{"state":"running","intent":intent["id"],
                "revision":intent.get("revision"),"reason":intent.get("reason"),"started_at":started_at})
            from .shared_learning import review_scope
            with review_scope(self, source.get("project") if source.get("evidence_mode") != "workflow-v1" else None), \
                    self.review_writes.capture(intent["handle"]) as calls, \
                    capture_review_summary(self.api, intent["handle"]) as summary:
                result = run_review(self.settings, self.api, intent["handle"], intent["id"], attempt_id)
            if self.settings.uses_scoped_storage:
                from .shared_storage import bind_skill_proposal
                for pending in result.get("staged", []):
                    if pending.get("tool") == "skill_manage":
                        bind_skill_proposal(self, source.get("project"), pending.get("result", {}).get("pending_id"), before_versions)
            curator_management = learning_dispatch.mark_user_taught(self, intent, result)
            if curator_management is not None:
                result = {**result, "curator_management": curator_management}
            finished_at = time.time()
            record_capacity_refusal(self.store, result, provenance["project"], finished_at)
            after_context = self.api.read(kind="context")
            after_versions = self.skill_versions(after_context)
            self.store.set("review-progress:"+intent["handle"],{"state":result["run_status"],"intent":intent["id"],
                "revision":intent.get("revision"),"started_at":started_at,"finished_at":finished_at,
                "reason":intent.get("reason"), "usage":result.get("usage"),
                "input":self.store.get("review-input:"+intent["id"],{}),"versions":{name:version for name,version in after_versions.items() if before_versions.get(name) != version},
                "candidates":result.get("learning_candidates",self.store.get("review-candidates:"+intent["id"],[])),
                "staged_count":len(result.get("staged",[])),"applied_count":len(result.get("applied",[]))})
            if source.get('evidence_mode') == 'workflow-v1':
                from .knowledge import changed_skills
                attestations = self.store.get('workflow-skill-attestations', {})
                catalog_versions = {item['name']: item.get('version') for item in after_context.get('skills', [])}
                for name in changed_skills(result):
                    if after_versions.get(name):
                        attestations[name] = {'version': after_versions[name],
                                              'catalog_version': catalog_versions.get(name),
                                              'attempt_id': attempt_id}
                self.store.set('workflow-skill-attestations', attestations)
            elif self.settings.data_policy:
                from .knowledge import changed_skills
                assessment = self.store.get("assessment:" + intent["handle"], {})
                if assessment.get("allowed"):
                    classification = assessment.get("classification") or (
                        "synthetic" if assessment.get("basis") == "synthetic_acceptance" else "approved")
                    proof = {"project":source.get("project"), "classification":classification,
                             "source_refs":assessment.get("source_refs", []), "attempt_id":attempt_id,
                             "policy_version":assessment.get("policy_version"),
                             "audience":assessment.get("audience"),"boundary_id":assessment.get("boundary_id")}
                    proofs = self.store.get("semantic-skill-attestations", {})
                    catalog_versions = {item["name"]:item.get("version") for item in after_context.get("skills", [])}
                    for name in changed_skills(result):
                        if after_versions.get(name):
                            previous_publication = self.store.get("publications-v1",{}).get(name,{})
                            proofs[name] = {**proof, "version":after_versions[name], "catalog_version":catalog_versions.get(name),
                                            "prior_published_version":previous_publication.get("artifact",{}).get("version")}
                            if result.get("review_protocol") == "native":
                                proofs[name].update(evidence_contract=0, review_handle=intent["handle"],
                                                    learning_evidence={"state": "native_review", "review_input": intent["id"]})
                            elif "learning_candidates" in result:
                                supported = [item for item in result["learning_candidates"] if item.get("state") == "accepted"
                                             and item.get("tool") == "skill_manage" and item.get("name") == name]
                                proofs[name].update(evidence_contract=1,review_handle=intent["handle"],learning_evidence={"state":"passed" if supported else "missing",
                                    "claims":supported,"review_input":intent["id"]})
                    self.store.set("semantic-skill-attestations", proofs)
                    if any(item.get("tool") == "memory" for item in result.get("applied", [])):
                        memory_proofs = self.store.get("semantic-memory-attestations", {})
                        before_keys = {key for key, _, _ in memory_entries(before_context)}
                        for key, _, _ in memory_entries(after_context):
                            if key not in before_keys:
                                memory_proofs[key] = proof
                        self.store.set("semantic-memory-attestations", memory_proofs)
            if self.settings.capture_scope == "global" or enterprise:
                self.update_knowledge(after_context, project=source.get("project"), result=result)
            if result["run_status"] not in {"deferred", "superseded"}:
                from .knowledge import changed_skills
                applied_entities = ["skill:" + name for name in changed_skills(result)
                                    if after_versions.get(name) and (name not in before_versions or
                                    (before_versions[name] and before_versions[name] != after_versions[name]))]
                if any(item.get("tool") == "memory" for item in result.get("applied", [])):
                    previous_entries = {key for key, _, _ in memory_entries(before_context)}
                    applied_entities += ["memory:" + key for key, _, _ in memory_entries(after_context) if key not in previous_entries]
                event = {**result, "id": attempt_id + ":result", "kind": "review.finished", **provenance,
                         "started_at": started_at, "finished_at": finished_at,
                         "applied_entities": sorted(set(applied_entities)), "review_summary": summary["text"]}
                if retry_available:
                    event["retry"] = self.retry_queue.record(intent, {**result, "id": attempt_id}, local)
                if result["run_status"] in {"failed", "interrupted"}:
                    failover = record_review_model_failure(
                        self, result.get("error"), failure_reason=result.get("failure_reason"))
                    if failover and failover.get("applied") and retry_available:
                        event["model_failover"] = {k: failover[k] for k in ("previous_model", "target_model", "requeued_blocked_reviews")}
                self.emit(event, calls=calls)
            self.store.set("state", "publishing")
            if result["run_status"] in {"completed","failed","interrupted","unknown","superseded"}:
                self.clear_reviewed_priority(intent,started_at)
            self.last_publish = 0.0
            learning_dispatch.record_result(self, intent, result, after_versions)
            # A long native review must not monopolize a whole batch of intents
            # while newer hook receipts wait in the persistent capture outbox.
            if result["run_status"] not in {"deferred","superseded"}:
                break

    def clear_reviewed_priority(self, intent, started_at):
        """A review of revision N cannot consume a newer turn's priority signal."""
        rows=self.store.db.execute("SELECT id FROM review_priority WHERE handle=? AND requested IS NOT NULL AND created<=?",
                                   (intent["handle"],started_at)).fetchall()
        for row in rows:
            receipt=self.store.get("observation-receipt:"+row["id"],{})
            revision=receipt.get("revision")
            if type(revision) is int and revision > intent.get("revision",-1):
                continue
            with self.store.db:
                self.store.db.execute("DELETE FROM review_priority WHERE id=?",(row["id"],))

    def skill_versions(self, context: dict) -> dict:
        cache = self.store.get("skill-versions-v1", {})
        versions = {}
        for item in context.get("skills", []):
            name = item["name"]
            cached = cache.get(name, {})
            if cached.get("catalog_version") == item.get("version") and cached.get("version"):
                versions[name] = cached["version"]
                continue
            try:
                value = native_reads.skill(self.api, name=name)
                versions[name] = value["version"]
                cache[name] = {"catalog_version": item.get("version"), "version": value["version"]}
            except (OSError, ValueError, KeyError):
                versions[name] = None
        self.store.set("skill-versions-v1", cache)
        return versions

    def idle_reviews(self) -> None:
        from .scheduling import flush
        flush(self.store, self.api, recovery_only=not self.settings.proactive)

    def latest_turns(self):
        latest = {}
        for row in self.store.db.execute("SELECT t.payload,t.stopped,t.activity FROM latest_turns l JOIN turns t ON t.id=l.turn_id ORDER BY t.activity"):
            data = json.loads(row["payload"])
            if self.store.get("excluded-chat:" + data["source"]["chat"]):
                continue
            latest[data["source"]["chat"]] = row
        return list(latest.values())

    def has_active_turns(self):
        now = time.time()
        cutoff = now - self.settings.active_turn_timeout_seconds
        for row in self.latest_turns():
            activity = row["activity"] if "activity" in row.keys() else None
            if isinstance(activity, (int, float)) and activity < cutoff:
                continue
            data = json.loads(row['payload'])
            if data['status'] == 'running':
                return True
            if (data['status'] == 'incomplete' and data.get('_ended_by') not in {'Stop', 'Interrupt'}
                    and not data.get('assistant')):
                return True
        return False  # Unknown tool outcomes do not reopen a terminated host turn.

    def reap_stale_turns(self) -> int:
        """Finalize abandoned running turns whose activity exceeded active_turn_timeout_seconds."""
        now = time.time()
        if now - getattr(self, "_last_stale_turn_reap", 0.0) < 60.0:
            return 0
        self._last_stale_turn_reap = now
        cutoff = now - self.settings.active_turn_timeout_seconds
        rows = self.store.db.execute(
            "SELECT id, payload FROM turns WHERE (stopped IS NULL OR stopped = 0) AND activity < ? "
            "AND json_extract(payload, '$.status') IN ('running', 'incomplete')",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        reaped = 0
        with self.store.db:
            for row in rows:
                data = json.loads(row["payload"])
                if data.get("status") not in {"running", "incomplete"}:
                    continue
                if data.get("_ended_by") in {"Stop", "Interrupt", "stale_timeout"}:
                    continue
                data["status"] = "incomplete"
                data["_ended_by"] = "stale_timeout"
                self.store.db.execute(
                    "UPDATE turns SET payload=?, stopped=COALESCE(stopped, ?) WHERE id=?",
                    (dumps(data), now, row["id"]),
                )
                reaped += 1
        return reaped

    def update_knowledge(self, context: dict, *, persist=True, **kwargs) -> dict:
        stored_index = self.store.get("knowledge-index-v1", {})
        index = stored_index
        if not index:
            # Migrate the previously single-project library once, without rewriting native files.
            index = reconcile({}, context, bootstrap_project=str(self.settings.project.resolve()))
            for attempt in self.api.read(kind="status").get("attempts", []):
                index = reconcile(index, context, project=str(self.settings.project.resolve()), result=attempt)
        updated = reconcile(index, context, bootstrap_project=str(self.settings.project.resolve()), **kwargs)
        for key, origin in self.store.get("approved-memory-origins", {}).items():
            if key in updated.get("memory", {}) and updated["memory"][key].get("project") in {None, origin["project"]}:
                updated["memory"][key] = {"project": origin["project"]}
        if persist and updated != stored_index:
            self.store.set("knowledge-index-v1", updated)
        return {**context, "bridge_provenance": updated}

    def publish(self) -> None:
        if time.monotonic() - self.last_publish < 10:
            return
        self.store.set('loop-progress', {'phase':'publishing', 'scope':'publication', 'updated_at':time.time()})
        from .lifecycle import synchronize
        page = synchronize(self)
        if page and page.get('has_more'):
            return  # Finish native history replay before replacing the recall view.
        context = self.api.read(kind="context")
        self.refresh_native_policy()
        from .lifecycle import recover_archived, reconcile_interrupted_curation
        reconcile_interrupted_curation(self, context)
        recover_archived(self, context)
        if self.settings.capture_scope == "global" or self.settings.data_policy.get("mode") in {"trusted_enterprise", "trusted_local"}:
            context = self.update_knowledge(context)
        with self.publication_lock:
            context = self.cache_artifacts(context)
            if self.settings.checked_publication:
                context = self.publish_checked(context)
            self.cache_context(context, self.store)
        atomic_json(self.settings.state / "status.json", {
            **native_reads.status(self.api), "daemon": self.store.get("state"), "updated_at": time.time(),
            "review_retries": self.retry_queue.summary(),
        })
        self.last_publish = time.monotonic()
        from .learning_dispatch import after_publish
        after_publish(self)

    def cache_context(self, context, store):
        from .enterprise import scope_view
        from .native_projection import sync_published_skills
        projection = sync_published_skills(self.settings, store)
        context=scope_view({**context,
            "native_projected_skills": projection["projected_skills"],
            "native_projection": {k: projection[k] for k in ("enabled", "catalog_dir", "codex_skills_dir", "projected_skills", "shadowed")},
            "enterprise_memory_boundaries":{key:proof["boundary_id"] for key,proof in store.get("semantic-memory-attestations",{}).items() if proof.get("boundary_id")},
            "enterprise_skill_boundaries":{key:proof["boundary_id"] for key,proof in store.get("semantic-skill-attestations",{}).items() if proof.get("boundary_id")}},
            self.settings.data_policy)
        atomic_json(self.settings.state / "context.json", context)

    def cache_artifacts(self, context):
        previous=self.store.get("artifact-read-cache",{})
        current={}
        retired = self.store.get('artifact-retired-at', {})
        for item in context.get("skills",[]):
            name=item["name"];cached=previous.get(name,{})
            if (cached.get("catalog_version")==item.get("version") and cached.get("artifact")
                    and Path(cached["artifact"].get("resource_dir","")).is_dir()):
                current[name]=cached
                continue
            try:
                artifact=native_reads.skill(self.api,name=name)
                current[name]={"artifact":{key:value for key,value in artifact.items() if key!="delivery_token"},
                               "catalog_version":item.get("version"), "cached_at":time.time()}
            except (OSError,ValueError):
                continue
        for name, record in previous.items():
            version = record.get('artifact', {}).get('version')
            if version and version != current.get(name, {}).get('artifact', {}).get('version'):
                retired.setdefault(version, time.time())
        self.store.set('artifact-retired-at', retired)
        self.store.set("artifact-read-cache",current)
        if "skills" not in context:
            return context
        return {**context, "skills": [
            {**item, "artifact_version": current.get(item["name"], {}).get("artifact", {}).get("version")}
            for item in context["skills"]
        ]}

    def refresh_native_policy(self):
        try:
            self.native_policy = native_policy()
        except (ImportError, OSError, TypeError, ValueError):
            self.native_policy = None

    def publish_checked(self, context: dict) -> dict:
        from .policy import admit, sharing_check
        from .enterprise import boundary, is_trusted_local
        from . import publication_guardian, publication_review
        from .quality import duplicate_key, proof_valid, verifier_hash, verify
        trusted_local = is_trusted_local(self.settings.data_policy)
        enterprise=boundary(self.settings.data_policy)
        if not isinstance(context.get("skills"), list):
            raise ValueError("Native skill catalog is unavailable; keep the previous publication")
        publications = self.store.get("publications-v1", {})
        self.publication_guardian.poll(self.store)
        active_policy = publication_guardian.policy(self.store)
        automatic = publication_review.enabled(self.settings) and active_policy["mode"] in ("smart", "autonomous")
        old_decisions = self.store.get("publication-decisions", {})
        rollbacks = self.store.get(publication_review.ROLLBACKS, {})
        publication_reviews = self.store.get("publication-reviews-v1", {})
        aliases = self.store.get("publication-aliases", {})
        present = {item["name"] for item in context.get("skills", [])}
        native_aliases = self.store.get('native-skill-aliases', {})
        publications = {name: item for name, item in publications.items() if name in present or name in native_aliases}
        publication_inputs = [context.get('version'), context.get('bridge_provenance'),
                                  self.store.get('workflow-skill-attestations', {}),
                                  self.store.get('semantic-skill-attestations', {}), self.settings.data_policy,
                                  enterprise["id"] if enterprise else None,
                                  self.settings.quality_checks, verifier_hash() if self.settings.quality_checks else None,
                                  native_aliases, self.store.get('lifecycle-withheld', {}),
                                  self.store.get('native-candidate-intents', {}),
                                  self.store.get('native-lifecycle-status', {}).get('state'), publication_reviews,
                                  [tuple(row) for row in self.store.db.execute("SELECT name,version FROM withheld_versions")]]
        if not self.settings.data_policy:
            publication_inputs.extend((active_policy, publication_guardian.POLICY_VERSION, rollbacks))
        publication_key = digest(publication_inputs)
        waiting = any(d.get("reason") == publication_guardian.WAITING for d in old_decisions.values())
        if self.store.get("publication-context-version") != publication_key or waiting:
            index = context.get("bridge_provenance", {})
            markers = []
            for _, _, entry in memory_entries(context):
                markers.extend(re.findall(r"[`\"']([^`\"']{5,120})[`\"']", entry))
                markers.extend(re.findall(r"\b[A-Z][A-Z0-9_-]{7,}\b", entry))
            decisions = {}
            review_candidates = {}
            history_updates = {}
            for item in context["skills"]:
                name = item["name"]
                origins = index.get("skills", {}).get(name, {}).get("projects", [])
                try:
                    value = native_reads.skill(self.api, name=name)
                    artifact_root = Path(value["resource_dir"]).resolve()
                    if not artifact_root.is_relative_to((self.settings.home / "learning/artifacts").resolve()):
                        raise ValueError("Artifact is outside the learning publication directory")
                    candidate = publication_review.identity(name, value, origins)
                    previous = publications.get(name, {})
                    reviewed = publication_reviews.get(name)
                    disposition = publication_review.disposition(reviewed, candidate)
                    intent = self.store.get('native-candidate-intents', {}).get(name, {})
                    native_review_candidate = (
                        trusted_local or (self.settings.review_protocol == "native" and bool(enterprise))
                    )
                    decision = sharing_check(value, origins, markers, executable_candidate=self.settings.quality_checks,
                                             enterprise=bool(enterprise or trusted_local),
                                             native_candidate=native_review_candidate or (intent.get('version') == value['version'] and bool(intent.get('sources'))),
                                             allow_absolute_paths=trusted_local)
                    if not trusted_local:
                        lifecycle_block = self.store.get('lifecycle-withheld', {}).get(name)
                        if lifecycle_block:
                            decision = {'allowed': False, **lifecycle_block}
                        elif self.store.get('native-lifecycle-status', {}).get('state') == 'history_gap':
                            proof = self.store.get('semantic-skill-attestations', {}).get(name, {})
                            if proof.get('version') != value['version']:
                                decision = {'allowed': False, 'reason': 'native_history_gap'}
                        required={proof.get("boundary_id") for proof in (
                            self.store.get("semantic-skill-attestations",{}).get(name,{}),
                            publications.get(name,{}).get("check",{})) if proof.get("boundary_id")}
                        if required and (not enterprise or required!={enterprise["id"]}):
                            decision={"allowed":False,"reason":"enterprise_boundary_not_active"}
                    if decision["allowed"]:
                        resources = {name: (artifact_root / name).read_text() for name in value["files"]}
                        origin_project = origins[0] if origins else str(self.settings.project)
                        assessment = admit({"kind": "shared_candidate", "resources": resources, "source_projects": origins},
                                           self.settings.data_policy or {"enabled": True}, origin_project, purpose="share")
                        if (not self.settings.data_policy and assessment.get('reason') == publication_review.UNASSESSED
                                and disposition == publication_review.APPROVE):
                            assessment = {"allowed": True, "basis": "version_bound_operator_review"}
                            decision = {**decision, "review_hash": reviewed["review_hash"]}
                        elif (not self.settings.data_policy and assessment.get('reason') == publication_review.UNASSESSED
                              and previous.get("check", {}).get("automatic_review")
                              and candidate == publication_review.identity(name, previous["artifact"], previous["sources"])):
                            assessment = {"allowed": True, "basis": "existing_automatic_publication"}
                            decision = {**decision, "automatic_review": previous["check"]["automatic_review"]}
                        from .workflow import PROJECT as WORKFLOW_PROJECT
                        attestation = self.store.get('workflow-skill-attestations', {}).get(name, {})
                        if (origins == [WORKFLOW_PROJECT] and name.startswith('workflow-')
                                and attestation.get('version') == value['version']):
                            assessment = {'allowed': True, 'basis': 'native_workflow_review_without_task_content'}
                        semantic = self.store.get("semantic-skill-attestations", {}).get(name, {})
                        evidence_ok = semantic.get("evidence_contract") != 1 or semantic.get("learning_evidence",{}).get("state") == "passed"
                        if semantic.get("version") == value["version"] and not evidence_ok:
                            assessment = {"allowed":False,"reason":"task_learning_evidence_missing"}
                        public_revision = (previous.get("artifact",{}).get("version") == semantic.get("prior_published_version")
                                           and bool(semantic.get("prior_published_version"))
                                           and set(origins) <= set(previous.get("sources",[])) | {semantic.get("project")})
                        if (evidence_ok and semantic.get("version") == value["version"] and (origins == [semantic.get("project")] or public_revision)
                                and semantic.get("classification") in {"public", "synthetic"}):
                            assessment = {"allowed":True, "basis":"scoped_assessed_native_review"}
                        if not assessment["allowed"]:
                            decision = assessment
                        elif enterprise:
                            decision={**decision,"audience":"enterprise","boundary_id":enterprise["id"]}
                        elif trusted_local:
                            decision={**decision,"audience":"local"}
                        if decision["allowed"] and semantic.get("evidence_contract") == 1:
                            decision={**decision,"task_relevance":{"state":"passed","proof_hash":digest(semantic.get("learning_evidence"))}}
                    if (disposition == publication_review.KEEP
                            and (decision['allowed'] or decision.get('reason') == publication_review.UNASSESSED)):
                        decision = {'allowed': False, 'reason': publication_review.KEPT_REASON,
                                    'review_hash': reviewed['review_hash']}
                    needs_review = (decision.get('reason') == publication_review.UNASSESSED and not self.settings.data_policy
                                    and len(origins) == 1 and self.settings.project_for(origins[0]) == origins[0]
                                    and publications.get(name, {}).get('artifact', {}).get('version') != value['version'])
                    if self.settings.quality_checks and (decision["allowed"] or needs_review):
                        key = "quality:" + digest([name, value["version"]])
                        proof = self.store.get(key, {})
                        verified = proof_valid(value, proof)
                        if needs_review and any(Path(path).suffix.lower() in {'.py', '.sh'} for path in value['files']):
                            if not verified or proof.get('kind') != 'isolated_execution':
                                proof = {'state': 'pending', 'reason': 'executable_resource_needs_validation'}
                        elif not verified:
                            proof = verify(value)
                            self.store.set(key, proof)
                        if proof["state"] != "passed":
                            decision = {"allowed":False,"reason":proof.get("reason", "quality_verification_failed"),
                                        "missing_sections": proof.get("missing_sections", [])}
                        else:
                            decision = {**decision, "validation":proof}
                    if self.store.db.execute("SELECT 1 FROM withheld_versions WHERE name=? AND version=?",
                                             (name, value["version"])).fetchone():
                        decision = {"allowed":False,"reason":"host_validation_failed"}
                    if value["version"] in rollbacks.get(name, {}):
                        decision = {"allowed": False, "reason": publication_review.ROLLED_BACK}
                    decision["version"] = value["version"]
                    if needs_review and decision.get("reason") == publication_review.UNASSESSED:
                        key = duplicate_key(value)
                        duplicate = next((other for other, record in publications.items()
                                          if other != name and duplicate_key(record["artifact"]) == key), None)
                        if duplicate:
                            decision["reason"] = "duplicate_of:" + duplicate
                        elif automatic:
                            outcome = self.publication_guardian.assess(
                                self.settings, self.store, active_policy, name, value, origins, previous)
                            if outcome is None:
                                decision["reason"] = publication_guardian.WAITING
                            elif outcome["verdict"] == "APPROVE":
                                decision.pop("reason")
                                decision.update(allowed=True, basis="independent_shared_skill_review",
                                                automatic_review={k: outcome[k] for k in (
                                                    "id", "baseline_version", "previous_version", "policy_version",
                                                    "reason", "at", "model") if k in outcome})
                            elif outcome["verdict"] == "DENY":
                                decision.update(reason=publication_review.AUTOMATIC_KEEP, review_reason=outcome["reason"],
                                                review_id=outcome["id"], review_at=outcome["at"])
                            else:
                                decision["review_reason"] = outcome["reason"]
                        if decision.get("reason") == publication_review.UNASSESSED:
                            review_candidates[name] = (digest(candidate), key)
                    if decision["allowed"]:
                        duplicate = next((other for other,record in publications.items() if other != name
                                          and duplicate_key(record["artifact"]) == duplicate_key(value)), None)
                        if duplicate:
                            aliases[name] = duplicate
                            publications.pop(name, None)
                            decisions[name] = {"allowed":False,"reason":"duplicate_of:"+duplicate,"version":value["version"]}
                            continue
                        aliases.pop(name, None)
                        if previous and previous["artifact"]["version"] != value["version"]:
                            history_key = "publication-history:" + name
                            history = self.store.get(history_key, {})
                            history[previous["artifact"]["version"]] = {**previous, 'retired_at': time.time()}
                            history_updates[history_key] = history
                        publications[name] = {"artifact": {key: val for key, val in value.items() if key != "delivery_token"},
                                              "catalog_entry": item, "sources": origins, "check": decision}
                    elif (name in publications and self.store.db.execute(
                            "SELECT 1 FROM withheld_versions WHERE name=? AND version=?",
                            (name, publications[name]["artifact"]["version"])).fetchone()):
                        history = self.store.get("publication-history:" + name, {})
                        previous = next((record for version, record in sorted(history.items(),
                                            key=lambda item: item[1].get("retired_at", 0), reverse=True)
                                         if version not in rollbacks.get(name, {}) and not self.store.db.execute(
                                             "SELECT 1 FROM withheld_versions WHERE name=? AND version=?", (name, version)).fetchone()), None)
                        if previous:
                            publications[name] = previous
                        else:
                            publications.pop(name, None)
                    decisions[name] = decision
                except (OSError, ValueError) as exc:
                    decisions[name] = {"allowed": False, "reason": type(exc).__name__}
            from .lifecycle import aliases_for
            forwarded = aliases_for(self.store, {name: item for name, item in publications.items() if name in present})
            aliases.update(forwarded)
            for name in forwarded:
                publications.pop(name, None)
            published_keys = {duplicate_key(record['artifact']): name for name, record in publications.items()}
            for name, (identifier, key) in review_candidates.items():
                duplicate = published_keys.get(key)
                if duplicate and duplicate != name:
                    decisions[name]['reason'] = 'duplicate_of:' + duplicate
                elif name not in aliases:
                    decisions[name]['review_candidate'] = identifier
            aliases = {name: target for name, target in aliases.items() if target in publications}
            for name, decision in decisions.items():
                identifier = decision.get("review_candidate")
                if not identifier and decision.get("reason") != publication_guardian.WAITING:
                    continue
                scope, = index["skills"][name]["projects"]
                prior = old_decisions.get(name, {})
                operator_review = publication_reviews.get(name, {}).get("review_hash")
                if (prior.get("review_scope") == scope and prior.get("review_notice")
                        and prior.get("last_operator_review") == operator_review):
                    decision["review_notice"] = prior["review_notice"]
                elif identifier:
                    decision["review_notice"] = "publication-review:" + identifier
                decision.update(review_scope=scope, last_operator_review=operator_review)
            with self.store.db:
                self.store.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                    ((key, dumps(val)) for key, val in {**history_updates, "publications-v1": publications,
                        "publication-aliases": aliases, "publication-decisions": decisions}.items()))
            for name, decision in decisions.items():
                sources = index.get("skills", {}).get(name, {}).get("projects", [])
                if (decision.get("allowed") or decision.get("reason") in {"project_experience",
                        publication_review.KEPT_REASON, publication_review.AUTOMATIC_KEEP, publication_review.ROLLED_BACK,
                        publication_guardian.WAITING}
                        or (automatic and decision.get("reason") == "project_private_material")
                        or decision.get("reason", "").startswith("duplicate_of:") or len(sources) != 1):
                    continue
                if identifier := decision.get("review_candidate"):
                    mark = "publication-reminder-issued:" + name
                    if self.store.get(mark) == decision["review_notice"]:
                        continue
                    self.emit({"id": decision["review_notice"], "kind": "approval.required",
                               "project": sources[0], "name": name, "approval_subject": "publication",
                               "review_candidate": identifier,
                               "pending_entities": ["publication:" + name], "project_approval": True,
                               "receipt": f"通用技能待审\n\n候选：{name}\n请打开「查看当前待审」核对最新版本及原因。"})
                    self.store.set(mark, decision["review_notice"])
                elif (not (automatic and decision.get("reason") == publication_review.UNASSESSED)
                      and old_decisions.get(name) != decision):
                    missing = [str(item) for item in decision.get("missing_sections") or [] if item]
                    receipt = f"通用候选：{name}\n状态：尚未发布\n原因：{decision.get('reason', 'unknown')}"
                    if missing:
                        receipt += "\n缺失项：" + "、".join(missing)
                    self.emit({"id": "pending-publication:" + digest([name, decision]), "kind": "publication.pending",
                               "project": sources[0], "name": name, "publication_reason": decision.get("reason"),
                               **({"missing_sections": missing} if missing else {}),
                               "receipt": receipt})
            self.store.set("publication-context-version", publication_key)
        for name, item in publications.items():
            version = item["artifact"]["version"]
            key = "publication-notified:" + name
            if self.store.get(key) != version:
                check = "自主判断通过" if item.get("check", {}).get("automatic_review") else "通过"
                self.emit({"id": "published:" + digest([name, version]), "kind": "publication.applied", "name": name,
                           "receipt": f"通用技能：{name}\n版本：{version}\n共享检查：{check}"})
                self.store.set(key, version)
        # Only the small catalog belongs in hook caches, never whole skill bodies.
        from .policy import publication_catalog
        visible = publication_catalog(publications, aliases)
        withheld = {(row[0],row[1]) for row in self.store.db.execute("SELECT name,version FROM withheld_versions")}
        catalog = []
        for item in context.get("skills", []):
            name = item["name"]
            if name in aliases:
                continue
            current_version = self.store.get("skill-versions-v1", {}).get(name, {}).get("version")
            if (name,current_version) in withheld:
                if name in publications:
                    catalog.append(visible[name]["catalog_entry"])
            else:
                catalog.append(item)
        # The old immutable version remains usable until the replacement passes
        # the same publication checks. A native archive does not invalidate it.
        for name in publications:
            if name not in present:
                catalog.append(visible[name]["catalog_entry"])
        return {**context, "skills":catalog, "checked_publication": True, "publications": visible}

    def notifications(self) -> None:
        if not self.settings.notifications or sys.platform != "darwin":
            return
        for event in self.store.consume("os-notification", 20):
            for notice in notification_events(event):
                if notice.get('approval_subject') == 'publication':
                    from .publication_review import pending
                    name = notice["name"]
                    current = self.store.get("publication-decisions", {}).get(name, {})
                    matches = current.get("review_notice") == notice["id"]
                    if not any(matches or digest(candidate) == notice.get('review_candidate') for candidate, *_ in pending(
                            self.settings, self.store, notice['project'], name=name)):
                        continue
                result = macos_notification(self.settings.state, notice)
                self.store.set("notification:" + notice["id"], result)

    def loop(self) -> None:
        from .shared_learning import drain as drain_shared_writes, drain_client, recover as recover_shared_writes
        migrate_shared = self.settings.uses_scoped_storage
        if self.resume.is_set():
            if migrate_shared:
                from .shared_storage import migrate
                recover_shared_writes(self)
                migrate(self, dry_run=False)
                migrate_shared = False
            from .feedback import reconcile as reconcile_feedback
            reconcile_feedback(self.store)
            self.publish()
        else:
            # Validate native reads without publishing candidate-derived provenance
            # into the live hook outbox before the activation commits.
            self.startup_context = self.api.read(kind="context")
            self.refresh_native_policy()
            if self.settings.capture_scope == "global":
                self.startup_context = self.update_knowledge(self.startup_context, persist=False)
            self.startup_status = native_reads.status(self.api)
        self.store.set("state", "idle" if self.resume.is_set() else "update-paused")
        self.ready.set()
        delay = 1
        last_completion_scan = float("-inf")
        while not self.stop.is_set():
            if self.drain.is_set():
                break
            if not self.resume.is_set():
                self.stop.wait(0.2)
                continue
            try:
                if migrate_shared:
                    from .shared_storage import migrate
                    recover_shared_writes(self)
                    migrate(self, dry_run=False)
                    migrate_shared = False
                from .review_resolution import process as resolve_reviews
                resolve_reviews(self)
                drain_client(self)
                self.ingest()
                drain_shared_writes(self)
                now = time.monotonic()
                if now - last_completion_scan >= 5:
                    last_completion_scan = now
                    from .host_evidence import recover_completions
                    recover_completions(self.settings, self.store)
                self.retry_completed_inputs()
                self.recover_context()
                from .learning_dispatch import prepare
                prepare(self)
                self.idle_reviews()
                if self.store.db.execute("SELECT 1 FROM events WHERE delivered IS NULL LIMIT 1").fetchone():
                    drain_started = self.store.get("backlog-drain-start") or time.time()
                    self.store.set("backlog-drain-start",drain_started)
                    if time.time()-drain_started >= 30:
                        self.reviews()
                        self.store.set("backlog-drain-start",time.time())
                else:
                    self.store.set("backlog-drain-start",None)
                    self.reviews()
                if self.drain.is_set():
                    break
                closing = self.store.get("closing", {})
                for event_id, handle in list(closing.items()):
                    if self.api.close(handle=handle, event_id=event_id)["status"] != "deferred":
                        del closing[event_id]
                self.store.set("closing", closing)
                self.reap_stale_turns()
                if not self.has_active_turns():
                    from .lifecycle import maintenance
                    maintenance(self)
                if self.store.get('native-curator-config-request'):
                    from .lifecycle import configure
                    configure(self)
                self.publish()
                if self.store.get("state") != "idle":
                    self.store.set("state", "idle")
                self.store.set("loop-progress",{**self.store.get("loop-progress",{}),"phase":"idle","updated_at":time.time()})
                self.notifications()
                if self.store.get("retention-request") or (self.settings.retention_enabled and
                        time.time() - self.store.get("retention:last", {}).get("at", 0) >= 86400):
                    from .retention import run as retain
                    retain(self.settings, self.store, self.api, dry_run=False)
                    self.store.set("retention-request", None)
                self.store.set("last_error", None)
                delay = 1
            except Exception as exc:
                self.store.set("last_error", f"{type(exc).__name__}: {exc}")
                self.store.set("state", "deferred")
                delay = min(delay * 2, 30)
            self.stop.wait(delay)

    def rpc(self, method: str, params: dict) -> dict:
        if method == "shared_approval_decision":
            if not self.settings.uses_scoped_storage:
                raise ValueError("Shared learning is disabled")
            from .shared_storage import approval_decision
            return approval_decision(self, self.request_project(params), params)
        if method.startswith("shared_"):
            from .shared_learning import rpc as shared_rpc
            return shared_rpc(self, method, params)
        from .learning_requests import METHODS, rpc as learning_rpc
        if method == "publication_review":
            from . import publication_review
            project = self.request_project({"cwd": params["cwd"]})
            action = params.get("action")
            target = (params.get("name"), params.get("revision"))
            if action in ("list", "file"):
                with StoreConnection(self.settings, read_only=True) as store, store.db:
                    store.db.execute('BEGIN')
                    if action == "list":
                        return publication_review.list_pending(self.settings, store, project)
                    return publication_review.preview_file(self.settings, store, project, *target, params.get("file"))
            if action in (publication_review.APPROVE, publication_review.KEEP):
                with self.publication_lock, StoreConnection(self.settings) as store:
                    if not self.resume.is_set() or self.drain.is_set():
                        raise RuntimeError("学习服务正在更新，请稍后刷新审批页。")
                    return publication_review.decide(self.settings, store, project, *target, action,
                                                     lambda name: native_reads.skill(self.api, name=name))
            if action == "rollback":
                from .policy import publication_catalog
                with self.publication_lock, StoreConnection(self.settings) as store:
                    if not self.resume.is_set() or self.drain.is_set():
                        raise RuntimeError("学习服务正在更新，请稍后操作。")
                    result = publication_review.rollback(self.settings, store, project, *target)
                    cache = self.settings.state / "context.json"
                    try:
                        if cache.exists():
                            context = json.loads(cache.read_text())
                            context["publications"] = publication_catalog(store.get("publications-v1", {}),
                                                                           store.get("publication-aliases", {}))
                            self.cache_context(context, store)
                        else:
                            from .native_projection import sync_published_skills
                            sync_published_skills(self.settings, store)
                    except (OSError, ValueError):
                        # The decision is durable. The owner's next publish rebuilds both views.
                        result["context_refresh_pending"] = True
                    return result
            raise ValueError("Unknown publication review action")
        if method == "publication_policy":
            from .publication_guardian import MODES, change_mode, policy
            from .publication_review import enabled
            mode = params.get("mode")
            if mode is None:
                with StoreConnection(self.settings, read_only=True) as store:
                    return {**policy(store), "supported": enabled(self.settings)}
            if mode not in MODES or (mode != "manual" and not enabled(self.settings)):
                raise ValueError("Automatic review requires checked personal-library publication")
            with self.publication_lock, StoreConnection(self.settings) as store:
                if not self.resume.is_set() or self.drain.is_set():
                    raise RuntimeError("学习服务正在更新，请稍后操作。")
                return change_mode(store, mode)
        if method == "review_publication":
            from .publication_review import submit
            project = self.request_project({"cwd": params["cwd"]})
            with self.publication_lock, StoreConnection(self.settings, read_only=params.get("dry_run", True)) as store:
                return submit(self.settings, store, project, params.get("review"), dry_run=params.get("dry_run", True))
        if method in METHODS:
            if "cwd" not in params:
                raise ValueError("Learning requests require the current workspace cwd")
            with StoreConnection(self.settings, read_only=method == "learning_request_status") as store:
                result = learning_rpc(store, self.settings, self.request_project(params), method, params)
                if method == "learning_request_status":
                    result["worker"] = {"ready":self.ready.is_set(), "paused":not self.resume.is_set(),
                                        "draining":self.drain.is_set(), "state":store.get("state")}
                return result
        if method == "pending_reviews":
            if self.settings.uses_scoped_storage:
                from .shared_storage import approval_list
                with StoreConnection(self.settings, read_only=True) as store:
                    return approval_list(self.settings, store, self.request_project(params), params.get("id"))
            from .approvals import PendingReviews
            with StoreConnection(self.settings, read_only=True) as store:
                return PendingReviews(self.settings, store).list(params.get("cwd"), params.get("id"))
        if method == "capabilities":
            return {"build":self.build,"native":self.native_capabilities}
        if method == 'configure_curator':
            if not self.native_capabilities.get('skill_lifecycle'):
                raise RuntimeError('Activate the verified native lifecycle patch first')
            if params.get('dry_run', True):
                return {'dry_run': True, 'changes': {'curator.enabled': True, 'curator.consolidate': True}}
            with StoreConnection(self.settings) as store:
                store.set('native-curator-config-request', {'at': time.time()})
            return {'state': 'queued'}
        if method == "review_retry":
            if not self.resume.is_set() or self.drain.is_set():
                raise RuntimeError("Learning runtime is paused; retry after the update")
            with StoreConnection(self.settings) as store:
                if params.get('inspection') is not None:
                    if not params.get('close_without_retry') or not self.native_capabilities.get('review_inspection'):
                        raise ValueError('Native inspection capability and explicit close-without-retry are required')
                    from .review_resolution import submit
                    return submit(store, self.request_project({'cwd': params['cwd']}), params['inspection'],
                                  dry_run=params.get('dry_run', True))
                queue = ReviewRetryQueue(self.settings, store)
                if params.get("close_without_retry") is True:
                    return queue.close_without_retry(params.get("attempt_id"), note=params.get("note"),
                                                     evidence=params.get("evidence"), dry_run=params.get("dry_run", True))
                return queue.request(params.get("attempt_id"), dry_run=params.get("dry_run", True))
        if method == "artifact_read":
            return native_reads.skill(self.api,name=params["name"])
        if method == "retention":
            from .retention import run as retain
            # Run mutations on the owner thread, never concurrently with a review.
            if not params.get("dry_run", True):
                with StoreConnection(self.settings) as store:
                    store.set("retention-request", {"at":time.time()})
                return {"state":"queued"}
            with StoreConnection(self.settings) as store:
                return retain(self.settings, store, self.api, dry_run=True)
        if method == "record_application":
            from .feedback import record
            with StoreConnection(self.settings) as store:
                return record(store, params["delivery_token"], self.request_project(params),
                              params["stage"], params.get("source_events", []))
        if method == "feedback_status":
            from .feedback import status
            project = self.request_project({"cwd": params["cwd"]})
            with StoreConnection(self.settings, read_only=True) as store:
                return status(store, project, params["chat"], params.get("turn"), params.get("receipt_id"))
        if method == "skill_requirements":
            from .skill_reads import load
            project = self.request_project({"cwd": params["cwd"]})
            if not params.get("artifact_version"):
                raise ValueError("Use the artifact version returned by recall or skill_load")
            return load(self.settings, params["name"], project, artifact_version=params["artifact_version"],
                        requirements_only=True)
        if method in {"memory_preview", "publication_preview"}:
            project = self.request_project({"cwd": params["cwd"]})
            path = self.settings.state / "context.json"
            context = self.startup_context if not self.resume.is_set() else (
                json.loads(path.read_text()) if path.is_file() else None)
            required_field = "memories" if method == "memory_preview" else "skills"
            if context is None or required_field not in context:
                return {"state": "snapshot_unavailable", "project": project}
            from .learning_health import memory_preview, publication_preview
            if method == "memory_preview":
                return memory_preview(self.settings, context, project, self.native_policy or {}, params.get("proposals"))
            with StoreConnection(self.settings, read_only=True) as store:
                return publication_preview(self.settings, store, context, project)
        if method == "quiesce_for_update":
            self.drain.set()
            return {"accepted": True, "mode": "finish_current_review"}
        if method == "resume_learning":
            self.resume.set()
            return {"accepted": True}
        if method == "wake":
            return {"accepted": True}
        if method == "shutdown":
            self.stop.set()
            self.api.cancel()
            return {"accepted": True}
        if method == "learning_status":
            status = self.settings.state / "status.json"
            with StoreConnection(self.settings, read_only=True) as store:
                snapshot = self.startup_status if not self.resume.is_set() else (
                    json.loads(status.read_text()) if status.exists() else None)
                context_path = self.settings.state / "context.json"
                context = self.startup_context if not self.resume.is_set() else (
                    json.loads(context_path.read_text()) if context_path.exists() else None)
                project = self.request_project(params) if "cwd" in params else None
                from .progress import summarize
                progress = summarize(store,project=project,
                    chat=params.get("chat"),turn=params.get("turn"),limit=params.get("limit",20),after_cursor=params.get("after_cursor",0))
                shared = None
                if self.settings.shared_profile is not None:
                    from .shared_learning import status as shared_status
                    shared = shared_status(store, project)
                from .native_projection import projection_status
                from .publication_guardian import policy
                from .publication_review import enabled
                return {"snapshot": snapshot, "shared_learning": shared,
                        "native_projection": projection_status(self.settings, store),
                        **learning_health(self.settings, store, context, project, self.native_policy),
                        "progress":progress,
                        "review_retries": ReviewRetryQueue(self.settings, store).summary(),
                        "build": self.build,
                        "plugin_root": str(Path(__file__).resolve().parents[2]),
                        "approval_capabilities": {"preview": True, "revision_checked_decisions": True,
                                                  "native_memory_only": True, "publication_reviews": True,
                                                  "publication_mode": policy(store)["mode"] if enabled(self.settings) else None,
                                                  "automatic_approval": False},
                        "coverage": {
                            "pending_events":store.db.execute("SELECT COUNT(*) FROM events WHERE delivered IS NULL").fetchone()[0],
                            "incomplete_turns":store.db.execute("SELECT COUNT(*) FROM turns WHERE json_extract(payload,'$.status')='incomplete'").fetchone()[0],
                            "high_value_pending":store.db.execute("SELECT COUNT(*) FROM review_priority "
                                "WHERE requested IS NULL AND reason<>'user_followup'").fetchone()[0],
                            "application_results":{row[0]:row[1] for row in store.db.execute(
                                "SELECT json_extract(payload,'$.validation'),COUNT(*) FROM application_evidence GROUP BY 1")}
                        },
                        "retention":store.get("retention:last"),
                        "native_curator": store.get('native-curator-status'),
                        "native_lifecycle": store.get('native-lifecycle-status'),
                        "native_curator_configuration": store.get('native-curator-config-result'),
                        "enterprise_policy":store.get("enterprise-policy-state"),
                        "pid": os.getpid(), "ready": self.ready.is_set(), "learning_api": self.api.version,
                        "source": str(self.settings.source),
                        "python": sys.executable,
                        "dependency_mode": "official_shared" if self.settings.fallback_python is not None
                            and Path(sys.executable) == self.settings.python else "private",
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
            if not self.resume.is_set() or (self.settings.uses_scoped_storage and not self.ready.is_set()):
                empty=context_view({"memories":{},"skills":[]},self.request_project(params))
                return {**empty,"available":False,"reason":"update_paused"}
            path = self.settings.state / "context.json"
            context = self.startup_context if not self.resume.is_set() else (
                json.loads(path.read_text()) if path.exists() else {"available": False})
            context = context or {"available": False}
            from .enterprise import scope_view, requires_scoping
            context=scope_view(context,self.settings.data_policy)
            if self.settings.uses_scoped_storage:
                from .shared_storage import scoped_context
                with StoreConnection(self.settings, read_only=True) as store:
                    return scoped_context(self.settings, store, context, self.request_project(params), params.get("query", ""))
            if self.settings.capture_scope == "global" or requires_scoping(context,self.settings.data_policy):
                return context_view(context, self.request_project(params), params.get("query", ""))
            return context
        if method == "skill_load":
            if not self.resume.is_set():
                raise RuntimeError("Learning runtime is paused for an update; retry shortly")
            from .skill_reads import load
            return load(self.settings, params["name"], self.request_project(params), params.get("file_path"),
                        artifact_version=params.get("artifact_version"))
        if method == "history_search":
            from .history_reads import search_history
            with StoreConnection(self.settings, read_only=True) as store:
                return search_history(
                    self.settings, store, self.request_project(params),
                    query=params.get("query", ""), session_id=params.get("session_id"),
                    limit=params.get("limit", 10), role_filter=params.get("role_filter", "user,assistant"),
                )
        if method == "projection_status":
            from .native_projection import projection_status
            with StoreConnection(self.settings, read_only=True) as store:
                return projection_status(self.settings, store)
        if method == "projection_sync":
            from .native_projection import sync_published_skills
            dry_run = bool(params.get("dry_run", True))
            with self.publication_lock, StoreConnection(self.settings, read_only=dry_run) as store:
                return sync_published_skills(self.settings, store, dry_run=dry_run)
        if method == "model_status":
            from .model_sync import model_status
            with StoreConnection(self.settings, read_only=True) as store:
                return model_status(self.settings, store)
        if method == "model_switch":
            from .model_sync import switch_pilot_model
            apply = bool(params.get("apply", False))
            with self.publication_lock, StoreConnection(self.settings, read_only=not apply) as store:
                queue = ReviewRetryQueue(self.settings, store) if apply else None
                return switch_pilot_model(
                    self.settings, store, params["model"],
                    requested_effort=params.get("effort"),
                    apply=apply, verify_probe=bool(params.get("verify_probe", True)),
                    pin=bool(params.get("pin", False)),
                    reason=params.get("reason") or "manual_switch",
                    retry_queue=queue, api=self.api if apply else None,
                )
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
    def __init__(self, settings, read_only=False):
        self.settings = settings
        self.read_only = read_only

    def __enter__(self):
        from .store import ReadStore
        self.store = ReadStore(self.settings.state) if self.read_only else Store(self.settings.state)
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
    try:
        bridge.native_capabilities = api.read(kind="capabilities")
    except (TypeError,ValueError):
        bridge.native_capabilities = {}
    if (settings.proactive or settings.quality_checks or settings.retention_enabled) and not all(
            bridge.native_capabilities.get(name) is True for name in ("artifact_preview","bounded_status","retention")):
        bridge.store.close()
        owner.close()
        raise RuntimeError("Cloud 0.5 learning policy requires its verified Learning API patch")
    if settings.shared_profile is not None and bridge.native_capabilities.get("shared_learning") != 1:
        bridge.store.close()
        owner.close()
        api.close()
        raise RuntimeError("The native runtime does not support shared learning protocol 1")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            from .shared_learning import Rejected
            try:
                request = json.loads(self.rfile.readline(1024 * 1024))
                result = {"result": bridge.rpc(request["method"], request.get("params", {}))}
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
                if isinstance(exc, Rejected):
                    result["permanent"] = True
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
        bridge.publication_guardian.close()
        api.close()
        bridge.store.close()
        owner.close()
    return 0
