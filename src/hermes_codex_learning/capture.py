from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from .config import Settings
from .delegation import exclude_internal
from .knowledge import context_view
from .store import Store, digest, dumps

ADDITIONAL_CONTEXT_LIMIT = 256


def model_context(memory: str, project: str | None, recommended: list[str]) -> tuple[str, list[str]]:
    """Fit whole skill names before allocating the remaining UTF-8 bytes to memory."""
    head = "Hermes 全局自动学习\n" if project else "Hermes 学习经验\n"
    tail = "按需查 hermes_learning.context(cwd=聊天工作区)"
    skills, offered = "", []
    for name in recommended:
        if not name or not name.isprintable() or name in offered:
            continue
        candidate = "相关技能 " + "、".join([*offered, name]) + "\n"
        if len((head + candidate + tail).encode("utf-8")) <= ADDITIONAL_CONTEXT_LIMIT:
            skills = candidate
            offered.append(name)
        if len(offered) == 3:
            break
    if project and project.isprintable():
        scoped_head = head + f"项目 {project}\n"
        if len((scoped_head + skills + tail).encode("utf-8")) <= ADDITIONAL_CONTEXT_LIMIT:
            head = scoped_head
    room = ADDITIONAL_CONTEXT_LIMIT - len((head + skills + tail).encode("utf-8"))
    preview = memory.encode("utf-8", errors="replace")[:max(0, room - 1)].decode("utf-8", errors="ignore").rstrip()
    if preview:
        preview += "\n"
    return head + skills + preview + tail, offered


def chat_workspace(store: Store, payload: dict) -> str | None:
    """Use lifecycle hooks for the chat workspace. Tool cwd is command-local."""
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    key = f"chat-workspace:{session}"
    cwd = payload.get("cwd")
    event = payload.get("hook_event_name")
    workspace = store.get(key)
    if event in {"SessionStart", "UserPromptSubmit"}:
        if isinstance(cwd, str) and Path(cwd).is_absolute():
            store.set(key, cwd)
            return cwd
        return None
    if workspace is None:
        # Recover resumed chats from durable pre-upgrade lifecycle receipts.
        row = store.db.execute(
            "SELECT json_extract(payload,'$.cwd') FROM events "
            "WHERE json_extract(payload,'$.session_id')=? "
            "AND json_extract(payload,'$.hook_event_name') IN ('SessionStart','UserPromptSubmit','Stop','Interrupt') "
            "ORDER BY created DESC LIMIT 1", (session,),
        ).fetchone()
        workspace = row[0] if row else None
        if workspace is None and event in {"Stop", "Interrupt"}:
            workspace = cwd
        if isinstance(workspace, str) and Path(workspace).is_absolute():
            store.set(key, workspace)
    return workspace


def hook(payload: dict, settings: Settings) -> dict:
    if payload.get("agent_id"):
        return {}
    command_cwd = payload.get("cwd")
    command_project = settings.project_for(command_cwd)
    if settings.capture_scope == "project" and command_project is None:
        return {}
    store = Store(settings.state)
    try:
        if exclude_internal(store, payload):
            return {}
        project = command_project
        if settings.capture_scope == "global":
            workspace = chat_workspace(store, payload)
            project = settings.project_for(workspace)
        if project is None or command_project is None:
            return {}
        from .policy import admit
        # Reserved fields are produced by this bridge, never accepted from a hook body.
        payload = {key: value for key, value in payload.items() if not key.startswith('bridge_')}
        host_context = {}
        if payload.get("transcript_path") and payload.get("hook_event_name") in {"SessionStart","UserPromptSubmit"}:
            from .host_evidence import read_host_relation
            try:
                host_context["relation"] = read_host_relation(Path(payload["transcript_path"]), payload["session_id"], project)
            except (OSError, ValueError, TypeError, KeyError):
                host_context["relation"] = {"status":"unavailable","reason":"host_relation_not_verified"}
            payload["bridge_host_context"] = host_context
        event = payload.get("hook_event_name")
        transient_query = str(payload.get("prompt") or "")[:4000] if event == "UserPromptSubmit" else ""
        technical = {"bridge_host_context":host_context} if host_context else {}
        if event in {"PreToolUse", "PostToolUse"}:
            from .workflow import action
            technical["bridge_action"] = action(payload)
            if settings.proactive and technical["bridge_action"] == "test":
                arguments = payload["tool_input"]
                technical["bridge_test_key"] = digest([command_cwd, arguments.get("command") or arguments.get("cmd")])
        if settings.proactive and event == "UserPromptSubmit":
            from .scheduling import record_dialogue_signals
            record_dialogue_signals(payload, store)
        from .delivery import capture_delivery
        capture_delivery(store, payload, project)
        admission = admit(payload, settings.data_policy, project)
        if admission.get("allowed") and admission.get("schema_version") == 2:
            payload = admission.pop("payload")
            payload["bridge_evidence_mode"] = "semantic-v2"
        if (not admission['allowed'] and admission.get("reason")!="explicitly_restricted_material"
                and settings.data_policy.get('workflow_projection') is True):
            from .workflow import project_event
            payload = project_event(payload)
            admission = {'allowed': True, 'basis': 'closed_vocabulary_workflow', 'body_retained': False}
        if not admission["allowed"]:
            if payload.get("session_id") and payload.get("turn_id"):
                store.set("withheld-turn:" + digest([payload["session_id"], payload["turn_id"]]), admission["reason"])
                store.set("capture-refusal:"+digest([payload["session_id"],payload["turn_id"]]),{
                    "project":project,"chat":payload["session_id"],"turn":payload["turn_id"],"at":time.time(),
                    "state":"withheld","reason":admission["reason"],"body_retained":False})
            gaps = store.get("coverage_gaps", [])
            store.set("coverage_gaps", (gaps + [{"event": payload.get("hook_event_name"),
                      "reason": admission["reason"], "body_retained": False}])[-100:])
            return {}
        payload = {**payload, **technical, "bridge_admission": admission,
                   'bridge_evidence_mode': payload.get('bridge_evidence_mode', 'full')}
        if payload.get("prompt") and admission.get("body_retained") is not False:
            from .evidence import selections
            selected = selections(payload["prompt"])
            if selected:
                payload["bridge_host_context"] = {**host_context, "selections":selected,
                                                   "selection_basis":"admitted_user_prompt"}
        payload = {**payload, "bridge_project": project, "bridge_host": store.get("host_id")}
        store.capture(payload)
        # Wake-up is best-effort; the durable outbox is polled after a restart.
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(0.05)
                client.connect(str(settings.socket))
                client.sendall(b'{"method":"wake"}\n')
        except (OSError, TimeoutError):
            pass
        event = payload.get("hook_event_name")
        if event not in {"SessionStart", "UserPromptSubmit", "PostToolUse"}:
            return {}
        cache_file = settings.state / "context.json"
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        from .enterprise import scope_view, requires_scoping
        cache=scope_view(cache,settings.data_policy)
        session = payload.get("session_id", "unknown")
        if event == "UserPromptSubmit":
            query = str(payload.get("prompt") or "")[:4000] if admission.get("body_retained") is not False else ""
            if not query and transient_query and cache.get("checked_publication"):
                # Local retrieval can select public catalog names without persisting
                # the unassessed query or sending it to the learning model.
                matches = context_view(cache, project, transient_query)
                shared = {item["name"] for item in matches["skills"] if item.get("scope") == "global"}
                query = " ".join(name for name in matches["recommended_skills"] if name in shared)
            store.set("recall-query:" + session, query)
        scoped = settings.capture_scope == "global" or requires_scoping(cache, settings.data_policy)
        query = store.get("recall-query:" + session, "")
        if scoped:
            cache = context_view(cache, project, query)
        recommended = cache.get("recommended_skills", [])
        if not scoped and cache.get("skills"):
            recommended = context_view(cache, project, query)["recommended_skills"]
        memory = ("\n".join(cache["memories"].values())[:340] if "memories" in cache
                  else cache.get("summary", ""))
        text, offered = model_context(memory, project if settings.capture_scope == "global" else None, recommended)
        version = cache.get("version")
        has_context = memory or offered or settings.capture_scope == "global"
        delivery_version = digest([version, text]) if version and has_context else None
        key = f"context-delivered:{session}:{project}" if settings.capture_scope == "global" else f"context-delivered:{session}"
        turn = payload.get("turn_id")
        if not delivery_version or (event != "UserPromptSubmit" and store.get(key) == delivery_version):
            return {}
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            returning_context = bool(delivery_version and store.get(key) != delivery_version)
            records = {key: delivery_version} if returning_context else {}
            if turn and (event == "UserPromptSubmit" or returning_context):
                offer_key = "recall-offer:" + digest([session, turn])
                previous = store.get(offer_key, {})
                offered_skills = previous.get("offered_skills")
                offered_versions = previous.get("offered_versions") or {}
                if returning_context:
                    offered_skills = list(dict.fromkeys([*(offered_skills or []), *offered]))
                    for skill in cache.get("skills", []):
                        artifact = skill.get("artifact_version")
                        if artifact and skill["name"] in offered:
                            versions = offered_versions.setdefault(skill["name"], [])
                            if artifact not in versions:
                                versions.append(artifact)
                elif not previous.get("context_returned"):
                    offered_skills = []
                records[offer_key] = {
                    "project": project, "at": time.time(), "names": recommended,
                    "context_returned": returning_context or previous.get("context_returned", False),
                    "offered_skills": offered_skills, "offered_versions": offered_versions, "version": version,
                }
            store.db.executemany(
                "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ((name, dumps(value)) for name, value in records.items()),
            )
        if returning_context:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": text,
                }
            }
        return {}
    finally:
        store.close()


def main() -> int:
    if os.environ.get("HERMES_LEARNING_MAINTENANCE") == "1":
        return 0
    try:
        settings = Settings.load()
    except FileNotFoundError:
        return 0  # An installed plugin is inert until a project is explicitly configured.
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Hook payload exceeds 16 MiB; capture coverage is incomplete")
        print(json.dumps(hook(json.loads(raw), settings), ensure_ascii=False))
        return 0
    except Exception as exc:
        # Hook failures never block business execution or request another model turn.
        print(f"Hermes learning capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
