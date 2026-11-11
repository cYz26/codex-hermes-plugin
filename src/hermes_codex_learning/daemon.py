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
from .normalize import merge_event, observation
from .notify import macos_notification, message, should_notify
from .review_receipt import ReviewWrites, receipt_text
from .store import Store, atomic_json, digest, dumps
from . import native_reads


class Bridge:
    def __init__(self, settings: Settings, api, review_writes: ReviewWrites | None = None):
        self.settings, self.api = settings, api
        self.review_writes = review_writes or ReviewWrites()
        self.store = Store(settings.state)
        from .read_receipts import initialize
        initialize(settings.state)
        self.stop = threading.Event()
        self.drain = threading.Event()
        self.ready = threading.Event()
        self.resume = threading.Event()
        self.startup_context = None
        self.startup_status = None
        from .identity import build_identity
        self.build = build_identity()
        self.last_publish = 0.0
        self.native_capabilities = {}
        if os.environ.get("HERMES_LEARNING_START_PAUSED") != "1":
            self.resume.set()
        self.store.set("state", "starting")

    def emit(self, event: dict) -> None:
        event = {**event, "message": message(event)}
        self.store.notice(event)
        from .notices import publish
        publish(self.settings, event)

    def handle_for(self, source: dict) -> str:
        result = self.api.open({**{k: source[k] for k in ("host", "project", "chat")}, "executor": "Codex App"})
        self.store.set("scope:" + result["handle"], {k: source[k] for k in ("host", "project", "chat")})
        self.bind_chat_handle(source, result["handle"])
        return result["handle"]

    def ingest(self) -> None:
        from .delivery import drain_deliveries
        drain_deliveries(self.store, self.usage)
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
        from .feedback import reconcile as reconcile_feedback
        reconcile_feedback(self.store)

    def observe_turn(self, data: dict, event_id: str) -> None:
        if data.get('_evidence_mode') == 'workflow-v1':
            self.observe_workflow(data, event_id)
            return
        if self.store.get("withheld-turn:" + digest([data["source"]["chat"], data["source"]["turn"]])):
            return
        enriched = task_input(self.store, data, settings=self.settings)
        if not enriched.get("user") or data["status"] in {"completed", "interrupted", "incomplete"}:
            from .host_evidence import supplement
            enriched = supplement(self.store, enriched, settings=self.settings)
        if self.settings.data_policy:
            from .policy import admit
            assessment = admit(observation(enriched), self.settings.data_policy, data["source"]["project"])
            if not assessment["allowed"]:
                return
            if self.settings.data_policy.get("mode")=="trusted_enterprise":
                from .enterprise import scrub
                enriched=scrub(enriched)
        else:
            assessment = {"allowed":True, "basis":"legacy_configuration"}
        if not enriched["user"]:
            return
        if enriched["status"] not in {"completed", "interrupted"}:
            if enriched != data:
                key = digest([enriched["source"]["chat"], enriched["source"]["turn"]])
                with self.store.db:
                    self.store.db.execute("UPDATE turns SET payload=?,revision=? WHERE id=?", (dumps(enriched), digest(enriched), key))
            return
        handle = self.handle_for(enriched["source"])
        from .enterprise import scope_boundary
        required=scope_boundary(self.store,handle)
        if required and required!=assessment.get("boundary_id"):
            self.store.set("policy-withheld:"+event_id,"enterprise_scope_not_active")
            return
        if assessment.get("boundary_id"):
            self.store.set("enterprise-scope:"+handle,assessment["boundary_id"])
        self.bind_chat_handle(enriched["source"], handle)
        self.store.set("assessment:" + handle, {key:value for key,value in assessment.items() if key != "payload"})
        self.api.observe(handle, assessment.get("payload", observation(enriched)), event_id=event_id)
        if self.settings.proactive:
            from .scheduling import enqueue
            enqueue(self.store, enriched, handle)
        revision = digest(enriched)
        key = digest([enriched["source"]["chat"], enriched["source"]["turn"]])
        with self.store.db:
            self.store.db.execute("UPDATE turns SET payload=?,revision=?,observed_revision=? WHERE id=?",
                                  (dumps(enriched), revision, revision, key))

    def observe_workflow(self, data: dict, event_id: str) -> None:
        from .workflow import observation as workflow_observation
        if data.get('_ended_by') not in {'Stop', 'Interrupt'}:
            return
        from .host_evidence import supplement
        data = supplement(self.store, data, settings=self.settings, include_task=False)
        data['status'] = 'interrupted' if data['_ended_by'] == 'Interrupt' else 'completed'
        value = workflow_observation(data)
        handle = self.api.open({k: value['source'][k] for k in ('host', 'project', 'chat', 'executor')})['handle']
        self.store.set('scope:' + handle, {**value['source'], 'evidence_mode': 'workflow-v1'})
        self.store.set('workflow-local-source:' + handle, data['source'])
        self.bind_chat_handle(data['source'], handle)
        self.api.observe(handle, value, event_id=event_id)
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
                                          "AND json_extract(payload,'$.hook_event_name') IN ('Stop','Interrupt') "
                                          "ORDER BY created DESC LIMIT 1", (data["source"]["chat"], data["source"]["turn"])).fetchone()
            if event:
                self.observe_turn(data, event["id"])

    def retry_completed_inputs(self) -> None:
        now = time.time()
        if now - self.store.get("last_input_retry", 0) < 30:
            return
        self.store.set("last_input_retry", now)
        rows = self.store.db.execute("SELECT payload FROM turns WHERE observed_revision IS NULL "
                                     "AND json_extract(payload,'$.status') IN ('completed','interrupted','incomplete') "
                                     "ORDER BY activity DESC LIMIT 8").fetchall()
        for row in rows:
            data = json.loads(row["payload"])
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
                self.store.set("issued-delivery:"+token,{**value,"project":payload["bridge_project"],"source_event":evidence["source_event"]})
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
        from .enterprise import MODE, boundary, scope_boundary
        enterprise=boundary(self.settings.data_policy)
        if self.settings.data_policy.get("mode")==MODE and not enterprise:
            self.store.set("enterprise-policy-state","boundary_changed")
            return
        self.store.set("enterprise-policy-state","ready" if enterprise else "not_enabled")
        status = native_reads.status(self.api)
        from .scheduling import ordered_intents
        for intent in ordered_intents(self.store, status["intents"]):
            if self.drain.is_set():
                break
            if intent["status"] != "queued":
                continue
            required=scope_boundary(self.store,intent["handle"])
            if required and (not enterprise or required!=enterprise["id"]):
                self.store.set("policy-withheld:"+intent["id"],"enterprise_boundary_not_active")
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
            local = self.store.get("workflow-local-source:" + intent["handle"], source)
            provenance = {"project": local.get("project"), "source_chat": local.get("chat")}
            before_context = self.api.read(kind="context")
            before_versions = self.skill_versions(before_context)
            if self.settings.capture_scope == "global" or enterprise:
                self.update_knowledge(before_context)
            started_at = time.time()
            self.emit({"id": attempt_id + ":start", "kind": "review.started", **provenance})
            self.store.set("state", "reviewing")
            with self.review_writes.capture(intent["handle"]) as calls:
                result = self.api.run_review(intent["handle"], intent["id"], attempt_id=attempt_id)
            finished_at = time.time()
            after_context = self.api.read(kind="context")
            after_versions = self.skill_versions(after_context)
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
                         "applied_entities": sorted(set(applied_entities))}
                if should_notify(event):
                    event["receipt"] = receipt_text({**event, "message": message(event)}, calls, finished_at,
                                                    started_at=started_at)
                self.emit(event)
            self.store.set("state", "publishing")
            if result["run_status"] in {"completed","failed","interrupted","unknown","superseded"}:
                with self.store.db:
                    self.store.db.execute("DELETE FROM review_priority WHERE handle=? AND requested IS NOT NULL AND created<=?",
                                          (intent["handle"],started_at))
            self.last_publish = 0.0

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
        if self.settings.proactive:
            from .scheduling import flush
            flush(self.store, self.api)
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
                request_key = "idle-reviewed:" + digest([data["source"]["chat"], data["source"]["turn"]])
                if self.store.get(request_key) == digest(data):
                    continue
                if data.get('_evidence_mode') == 'workflow-v1':
                    from .workflow import observation as workflow_observation
                    source = workflow_observation(data)['source']
                    handle = self.api.open({k: source[k] for k in ('host', 'project', 'chat', 'executor')})['handle']
                else:
                    handle = self.handle_for(data['source'])
                self.api.maintenance(handle=handle, idle_flush=True)
                self.store.set(request_key, digest(data))
        self.store.set("last_idle_request", now)

    def latest_turns(self):
        latest = {}
        for row in self.store.db.execute("SELECT t.payload,t.stopped,t.activity FROM latest_turns l JOIN turns t ON t.id=l.turn_id ORDER BY t.activity"):
            data = json.loads(row["payload"])
            if self.store.get("excluded-chat:" + data["source"]["chat"]):
                continue
            latest[data["source"]["chat"]] = row
        return list(latest.values())

    def has_active_turns(self):
        return bool(self.store.db.execute(
            "SELECT 1 FROM latest_turns l JOIN turns t ON t.id=l.turn_id "
            "WHERE json_extract(t.payload,'$.status') IN ('running','incomplete') LIMIT 1").fetchone())

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
        context = self.api.read(kind="context")
        if self.settings.capture_scope == "global" or self.settings.data_policy.get("mode")=="trusted_enterprise":
            context = self.update_knowledge(context)
        self.cache_artifacts(context)
        if self.settings.checked_publication:
            context = self.publish_checked(context)
        from .enterprise import scope_view
        context=scope_view({**context,
            "enterprise_memory_boundaries":{key:proof["boundary_id"] for key,proof in self.store.get("semantic-memory-attestations",{}).items() if proof.get("boundary_id")},
            "enterprise_skill_boundaries":{key:proof["boundary_id"] for key,proof in self.store.get("semantic-skill-attestations",{}).items() if proof.get("boundary_id")}},
            self.settings.data_policy)
        atomic_json(self.settings.state / "context.json", context)
        atomic_json(self.settings.state / "status.json", {
            **native_reads.status(self.api), "daemon": self.store.get("state"), "updated_at": time.time(),
        })
        self.last_publish = time.monotonic()

    def cache_artifacts(self, context):
        previous=self.store.get("artifact-read-cache",{})
        current={}
        for item in context.get("skills",[]):
            name=item["name"];cached=previous.get(name,{})
            if (cached.get("catalog_version")==item.get("version") and cached.get("artifact")
                    and Path(cached["artifact"].get("resource_dir","")).is_dir()):
                current[name]=cached
                continue
            try:
                artifact=native_reads.skill(self.api,name=name)
                current[name]={"artifact":{key:value for key,value in artifact.items() if key!="delivery_token"},
                               "catalog_version":item.get("version")}
            except (OSError,ValueError):
                continue
        self.store.set("artifact-read-cache",current)

    def publish_checked(self, context: dict) -> dict:
        from .policy import admit, sharing_check
        from .enterprise import boundary
        enterprise=boundary(self.settings.data_policy)
        if not isinstance(context.get("skills"), list):
            raise ValueError("Native skill catalog is unavailable; keep the previous publication")
        publications = self.store.get("publications-v1", {})
        aliases = self.store.get("publication-aliases", {})
        present = {item["name"] for item in context.get("skills", [])}
        publications = {name: item for name, item in publications.items() if name in present}
        from .quality import verifier_hash
        publication_key = digest([context.get('version'), context.get('bridge_provenance'),
                                  self.store.get('workflow-skill-attestations', {}),
                                  self.store.get('semantic-skill-attestations', {}), self.settings.data_policy,
                                  enterprise["id"] if enterprise else None,
                                  self.settings.quality_checks, verifier_hash() if self.settings.quality_checks else None,
                                  [tuple(row) for row in self.store.db.execute("SELECT name,version FROM withheld_versions")]])
        if self.store.get("publication-context-version") != publication_key:
            index = context.get("bridge_provenance", {})
            markers = []
            for _, _, entry in memory_entries(context):
                markers.extend(re.findall(r"[`\"']([^`\"']{5,120})[`\"']", entry))
                markers.extend(re.findall(r"\b[A-Z][A-Z0-9_-]{7,}\b", entry))
            decisions = {}
            for item in context.get("skills", []):
                name = item["name"]
                origins = index.get("skills", {}).get(name, {}).get("projects", [])
                try:
                    value = native_reads.skill(self.api, name=name)
                    artifact_root = Path(value["resource_dir"]).resolve()
                    if not artifact_root.is_relative_to((self.settings.home / "learning/artifacts").resolve()):
                        raise ValueError("Artifact is outside the learning publication directory")
                    decision = sharing_check(value, origins, markers, executable_candidate=self.settings.quality_checks,
                                             enterprise=bool(enterprise))
                    required={proof.get("boundary_id") for proof in (
                        self.store.get("semantic-skill-attestations",{}).get(name,{}),
                        publications.get(name,{}).get("check",{})) if proof.get("boundary_id")}
                    if required and (not enterprise or required!={enterprise["id"]}):
                        decision={"allowed":False,"reason":"enterprise_boundary_not_active"}
                    if decision["allowed"]:
                        resources = {name: (artifact_root / name).read_text() for name in value["files"]}
                        assessment = admit({"kind": "shared_candidate", "resources": resources, "source_projects": origins},
                                           self.settings.data_policy or {"enabled": True}, origins[0], purpose="share")
                        from .workflow import PROJECT as WORKFLOW_PROJECT
                        attestation = self.store.get('workflow-skill-attestations', {}).get(name, {})
                        if (origins == [WORKFLOW_PROJECT] and name.startswith('workflow-')
                                and attestation.get('version') == value['version']):
                            assessment = {'allowed': True, 'basis': 'native_workflow_review_without_task_content'}
                        semantic = self.store.get("semantic-skill-attestations", {}).get(name, {})
                        previous = publications.get(name,{})
                        public_revision = (previous.get("artifact",{}).get("version") == semantic.get("prior_published_version")
                                           and bool(semantic.get("prior_published_version"))
                                           and set(origins) <= set(previous.get("sources",[])) | {semantic.get("project")})
                        if (semantic.get("version") == value["version"] and (origins == [semantic.get("project")] or public_revision)
                                and semantic.get("classification") in {"public", "synthetic"}):
                            assessment = {"allowed":True, "basis":"scoped_assessed_native_review"}
                        if not assessment["allowed"]:
                            decision = assessment
                        elif enterprise:
                            decision={**decision,"audience":"enterprise","boundary_id":enterprise["id"]}
                    if self.settings.quality_checks and decision["allowed"]:
                        from .quality import proof_valid, verify
                        key = "quality:" + digest([name, value["version"]])
                        proof = self.store.get(key, {})
                        if not proof_valid(value, proof):
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
                    decision["version"] = value["version"]
                    if decision["allowed"]:
                        from .quality import duplicate_key
                        duplicate = next((other for other,record in publications.items() if other != name
                                          and duplicate_key(record["artifact"]) == duplicate_key(value)), None)
                        if duplicate:
                            aliases[name] = duplicate
                            publications.pop(name, None)
                            decisions[name] = {"allowed":False,"reason":"duplicate_of:"+duplicate,"version":value["version"]}
                            continue
                        aliases.pop(name, None)
                        history = self.store.get("publication-history:" + name, {})
                        if name in publications:
                            history[publications[name]["artifact"]["version"]] = publications[name]
                        self.store.set("publication-history:" + name, history)
                        publications[name] = {"artifact": {key: val for key, val in value.items() if key != "delivery_token"},
                                              "catalog_entry": item, "sources": origins, "check": decision}
                    elif (name in publications and self.store.db.execute(
                            "SELECT 1 FROM withheld_versions WHERE name=? AND version=?",
                            (name, publications[name]["artifact"]["version"])).fetchone()):
                        history = self.store.get("publication-history:" + name, {})
                        previous = next((record for version,record in reversed(list(history.items()))
                                         if not self.store.db.execute("SELECT 1 FROM withheld_versions WHERE name=? AND version=?",
                                                                      (name,version)).fetchone()), None)
                        if previous:
                            publications[name] = previous
                        else:
                            publications.pop(name, None)
                    decisions[name] = decision
                except (OSError, ValueError) as exc:
                    decisions[name] = {"allowed": False, "reason": type(exc).__name__}
            self.store.set("publications-v1", publications)
            self.store.set("publication-aliases", {name:target for name,target in aliases.items() if target in publications})
            self.store.set("publication-decisions", decisions)
            self.store.set("publication-context-version", publication_key)
            for name, decision in decisions.items():
                sources = index.get("skills", {}).get(name, {}).get("projects", [])
                if (decision.get("allowed") or decision.get("reason") == "project_experience"
                        or decision.get("reason", "").startswith("duplicate_of:") or len(sources) != 1):
                    continue
                self.emit({"id": "pending-publication:" + digest([name, decision]), "kind": "publication.pending",
                           "project": sources[0], "name": name,
                           "receipt": f"通用候选：{name}\n状态：尚未发布\n原因：{decision.get('reason', 'unknown')}"})
        for name, item in publications.items():
            version = item["artifact"]["version"]
            key = "publication-notified:" + name
            if self.store.get(key) != version:
                self.emit({"id": "published:" + digest([name, version]), "kind": "publication.applied", "name": name,
                           "receipt": f"通用技能：{name}\n版本：{version}\n共享检查：通过"})
                self.store.set(key, version)
        # Only the small catalog belongs in hook caches, never whole skill bodies.
        visible = {name: {"catalog_entry": {**item["catalog_entry"],"aliases":[key for key,target in aliases.items() if target == name]},
                          "version": item["artifact"]["version"],"audience":item.get("check",{}).get("audience","public"),
                          "boundary_id":item.get("check",{}).get("boundary_id")}
                   for name, item in publications.items()}
        withheld = {(row[0],row[1]) for row in self.store.db.execute("SELECT name,version FROM withheld_versions")}
        catalog = []
        for item in context.get("skills", []):
            name = item["name"]
            if name in aliases:
                continue
            current_version = self.store.get("skill-versions-v1", {}).get(name, {}).get("version")
            if (name,current_version) in withheld:
                if name in publications:
                    catalog.append(publications[name]["catalog_entry"])
            else:
                catalog.append(item)
        return {**context, "skills":catalog, "checked_publication": True, "publications": visible}

    def notifications(self) -> None:
        if not self.settings.notifications or sys.platform != "darwin":
            return
        for event in self.store.consume("os-notification", 20):
            if should_notify(event):
                result = macos_notification(self.settings.state, event)
                self.store.set("notification:" + event["id"], result)

    def loop(self) -> None:
        if self.resume.is_set():
            self.publish()
        else:
            # Validate native reads without publishing candidate-derived provenance
            # into the live hook outbox before the activation commits.
            self.startup_context = self.api.read(kind="context")
            if self.settings.capture_scope == "global":
                self.startup_context = self.update_knowledge(self.startup_context, persist=False)
            self.startup_status = native_reads.status(self.api)
        self.store.set("state", "idle" if self.resume.is_set() else "update-paused")
        self.ready.set()
        delay = 1
        while not self.stop.is_set():
            if self.drain.is_set():
                break
            if not self.resume.is_set():
                self.stop.wait(0.2)
                continue
            try:
                self.ingest()
                self.retry_completed_inputs()
                self.recover_context()
                self.idle_reviews()
                self.reviews()
                if self.drain.is_set():
                    break
                closing = self.store.get("closing", {})
                for event_id, handle in list(closing.items()):
                    if self.api.close(handle=handle, event_id=event_id)["status"] != "deferred":
                        del closing[event_id]
                self.store.set("closing", closing)
                if not self.has_active_turns():
                    self.api.maintenance()
                self.publish()
                if self.store.get("state") != "idle":
                    self.store.set("state", "idle")
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
        if method == "pending_reviews":
            from .approvals import PendingReviews
            with StoreConnection(self.settings, read_only=True) as store:
                return PendingReviews(self.settings, store).list(params.get("cwd"), params.get("id"))
        if method == "capabilities":
            return {"build":self.build,"native":self.native_capabilities}
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
                return {"snapshot": snapshot,
                        "build": self.build,
                        "plugin_root": str(Path(__file__).resolve().parents[2]),
                        "approval_capabilities": {"preview": True, "revision_checked_decisions": True,
                                                  "native_memory_only": True, "automatic_approval": False},
                        "coverage": {
                            "pending_events":store.db.execute("SELECT COUNT(*) FROM events WHERE delivered IS NULL").fetchone()[0],
                            "incomplete_turns":store.db.execute("SELECT COUNT(*) FROM turns WHERE json_extract(payload,'$.status')='incomplete'").fetchone()[0],
                            "high_value_pending":store.db.execute("SELECT COUNT(*) FROM review_priority WHERE requested IS NULL").fetchone()[0],
                            "application_results":{row[0]:row[1] for row in store.db.execute(
                                "SELECT json_extract(payload,'$.validation'),COUNT(*) FROM application_evidence GROUP BY 1")}
                        },
                        "retention":store.get("retention:last"),
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
            if not self.resume.is_set():
                empty=context_view({"memories":{},"skills":[]},self.request_project(params))
                return {**empty,"available":False,"reason":"update_paused"}
            path = self.settings.state / "context.json"
            context = self.startup_context if not self.resume.is_set() else (
                json.loads(path.read_text()) if path.exists() else {"available": False})
            context = context or {"available": False}
            from .enterprise import scope_view, requires_scoping
            context=scope_view(context,self.settings.data_policy)
            if self.settings.capture_scope == "global" or requires_scoping(context,self.settings.data_policy):
                return context_view(context, self.request_project(params), params.get("query", ""))
            return context
        if method == "skill_load":
            if not self.resume.is_set():
                raise RuntimeError("Learning runtime is paused for an update; retry shortly")
            from .skill_reads import load
            return load(self.settings,params["name"],self.request_project(params),params.get("file_path"))
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
