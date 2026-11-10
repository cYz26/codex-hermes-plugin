from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from .config import Settings
from .delegation import exclude_internal
from .knowledge import context_view
from .store import Store, digest

ADDITIONAL_CONTEXT_LIMIT = 256


def model_context(summary: str, project: str | None) -> str:
    """Bound the complete payload by bytes, an upper bound for byte-level BPE tokens."""
    head = "Hermes 学习经验\n"
    tail = "\n参考。按需查 hermes_learning.context(cwd=聊天项目路径)"
    if project and project.isprintable():
        scoped_head = f"Hermes 全局自动学习已启用；当前项目：{project}\n"
        scoped_tail = "\n参考。按需查 hermes_learning.context(cwd=上方路径)"
        if len((scoped_head + scoped_tail).encode("utf-8")) <= ADDITIONAL_CONTEXT_LIMIT:
            head, tail = scoped_head, scoped_tail
    room = ADDITIONAL_CONTEXT_LIMIT - len((head + tail).encode("utf-8"))
    preview = summary.encode("utf-8", errors="replace")[:room].decode("utf-8", errors="ignore")
    return head + preview + tail


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
        transient_query = str(payload.get("prompt") or "")[:4000] if payload.get("hook_event_name") == "UserPromptSubmit" else ""
        technical = {}
        if settings.proactive:
            from .scheduling import annotate
            annotated = annotate(payload, store)
            technical = {key:val for key,val in annotated.items() if key.startswith("bridge_")}
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
            gaps = store.get("coverage_gaps", [])
            store.set("coverage_gaps", (gaps + [{"event": payload.get("hook_event_name"),
                      "reason": admission["reason"], "body_retained": False}])[-100:])
            return {}
        payload = {**payload, **technical, "bridge_admission": admission,
                   'bridge_evidence_mode': payload.get('bridge_evidence_mode', 'full')}
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
                query = " ".join(item["name"] for item in matches["skills"] if item.get("scope") == "global")
            store.set("recall-query:" + session, query)
        if settings.capture_scope == "global" or requires_scoping(cache,settings.data_policy):
            cache = context_view(cache, project, store.get("recall-query:" + session, ""))
        version = cache.get("version")
        key = f"context-delivered:{session}:{project}" if settings.capture_scope == "global" else f"context-delivered:{session}"
        if version and (cache.get("summary") or settings.capture_scope == "global") and store.get(key) != version:
            text = model_context(cache.get("summary", ""), project if settings.capture_scope == "global" else None)
            store.set(key, version)
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
