"""Scope native stores and tools to the bridge identity that owns the operation."""
from __future__ import annotations

import json
import time
from contextlib import closing, contextmanager
from pathlib import Path

from . import native_reads
from .knowledge import ENTRY_DELIMITER, context_view, memory_entries
from .store import Store, digest, dumps, redact


TARGETS = {"memory": "MEMORY.md", "user": "USER.md"}


def scope_id(project):
    return digest(["project", project]) if project else "personal"


def directories(settings, project):
    root = settings.home / "shared-knowledge"
    project_dir = root / scope_id(project)
    personal = root / "personal"
    if root.is_symlink() or project_dir.is_symlink() or personal.is_symlink():
        raise PermissionError("Shared knowledge directories must not be symlinks")
    return project_dir, personal


def memory_path(settings, project, target):
    if target == "memory" and project is None:
        raise PermissionError("Project memory requires the current workspace")
    scoped, personal = directories(settings, project)
    path = (personal if target == "user" else scoped) / "memories" / TARGETS[target]
    if path.is_symlink() or path.parent.is_symlink():
        raise PermissionError("Shared memory must not be symlinked")
    return path


def _raw_entries(path):
    from tools.memory_tool import MemoryStore

    raw, readable = MemoryStore._read_raw_checked(path)
    if not readable:
        raise OSError("Shared memory could not be read")
    return list(dict.fromkeys(MemoryStore._parse_entries(raw)))


def _target_scope(project, target):
    return scope_id(project if target == "memory" else None)


def _removals(store, project, target):
    return {row[0] for row in store.db.execute("SELECT digest FROM shared_memory_removals WHERE scope=? AND target=?",
                                             (_target_scope(project, target), target))}


def _memory_boundary_key(project, target, item_digest):
    return f"{_target_scope(project, target)}:{target}:{item_digest}"


def _visible_entries(settings, store, project, target, entries):
    from .enterprise import MODE, boundary, is_trusted_local
    trusted_local = is_trusted_local(settings.data_policy)
    active = boundary(settings.data_policy)
    if settings.data_policy.get("mode") == MODE and not active:
        return []
    removed = _removals(store, project, target)
    enterprise_map = store.get("shared-enterprise-memory-boundaries", {})
    visible = []
    for entry in dict.fromkeys(entries):
        item_digest = digest(entry)
        if item_digest in removed:
            continue
        required = enterprise_map.get(_memory_boundary_key(project, target, item_digest))
        if not trusted_local and required and (not active or active["id"] != required):
            continue
        visible.append(entry)
    return visible


def _entries(settings, store, project, target):
    if project is None and target == "memory":
        return []
    return _visible_entries(settings, store, project, target, _raw_entries(memory_path(settings, project, target)))


def memory_view(settings, store, project):
    targets = ("memory", "user") if project else ("user",)
    entries = {target: _entries(settings, store, project, target) for target in targets}
    return {"memories": {TARGETS[t]: ENTRY_DELIMITER.join(es) for t, es in entries.items() if es},
            "memory_versions": {t: digest(es) for t, es in entries.items()},
            "memory_scopes": {"USER.md": "user", **({"MEMORY.md": "project"} if project else {})}}


def _guard_memory(settings, project, target, before, after):
    from .enterprise import MODE, boundary
    if settings.data_policy.get("mode") == MODE and not boundary(settings.data_policy):
        return "Enterprise learning boundary is unavailable or changed"
    with closing(Store(settings.state)) as store:
        if store.get(_intent_key(project, target)):
            return "An earlier memory write requires recovery"
        removed = _removals(store, project, target)
        if any(digest(item) in removed for item in set(after) - set(before)):
            return "A removed memory cannot be reintroduced by an older request or review"
    return None


def _intent_key(project, target):
    return f"shared-memory-intent:{_target_scope(project, target)}:{target}"


def _finish_memory_intent(settings, store, key, record):
    target, project = record["target"], record["project"]
    current = digest(_raw_entries(memory_path(settings, project, target)))
    if current not in {record["before"], record["after"]}:
        return False
    with store.db:
        if current == record["after"]:
            if record.get("removed"):
                store.db.executemany("INSERT OR IGNORE INTO shared_memory_removals VALUES (?,?,?)",
                                     ((_target_scope(project, target), target, item) for item in record["removed"]))
            if record.get("added") and record.get("enterprise_boundary"):
                boundaries = store.get("shared-enterprise-memory-boundaries", {})
                for item in record["added"]:
                    boundaries[_memory_boundary_key(project, target, item)] = record["enterprise_boundary"]
                store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                 ("shared-enterprise-memory-boundaries", dumps(boundaries)))
        store.db.execute("DELETE FROM meta WHERE key=?", (key,))
    return True


def recover_memory_intents(settings, store):
    rows = store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'shared-memory-intent:%'").fetchall()
    for key, data in rows:
        _finish_memory_intent(settings, store, key, json.loads(data))


@contextmanager
def _commit_memory(settings, project, target, before, after):
    from .enterprise import boundary
    approved = boundary(settings.data_policy)
    removed = sorted(digest(entry) for entry in set(before) - set(after))
    added = sorted(digest(entry) for entry in set(after) - set(before)) if approved else []
    if not removed and not added:
        yield
        return
    key = _intent_key(project, target)
    with closing(Store(settings.state)) as store:
        record = {"target": target, "project": project if target == "memory" else None,
                  "before": digest(_raw_entries(memory_path(settings, project, target))),
                  "after": digest(after), "removed": removed,
                  **({"added": added, "enterprise_boundary": approved["id"]} if added else {})}
        store.set(key, record)
        try:
            yield
        finally:
            _finish_memory_intent(settings, store, key, record)


def _version(bridge, name):
    try:
        return native_reads.skill(bridge.api, name=name)["version"]
    except (ValueError, OSError, KeyError):
        return None


def _skill_filters(bridge, project, *, store=None):
    settings = bridge.settings
    store = store if store is not None else bridge.store
    index = store.get("knowledge-index-v1", {}).get("skills", {})
    published = store.get("publications-v1", {})
    retired = {r[0] for r in store.db.execute("SELECT name FROM shared_skill_removals")}
    known = {item["name"] for item in bridge.api.read(kind="context").get("skills", [])}
    created = set()
    from .enterprise import MODE, boundary, is_trusted_local
    trusted_local = is_trusted_local(settings.data_policy)
    active_boundary = boundary(settings.data_policy)
    enterprise_mode = settings.data_policy.get("mode") == MODE

    def own(name):
        return project is not None and index.get(name, {}).get("projects") == [project]

    def shared_editable(name):
        if not settings.allow_cross_project_shared_skill_edits:
            return False
        pub = published.get(name)
        if not pub:
            return False
        if trusted_local:
            return True
        check = pub.get("check") or {}
        if check.get("audience") == "enterprise":
            if not active_boundary or check.get("boundary_id") != active_boundary["id"]:
                return False
        elif enterprise_mode and not active_boundary:
            return False
        return True

    def can_edit(action, raw, absorbed):
        if not project or not isinstance(raw, str):
            return False
        name = Path(raw).name
        if name in retired or (absorbed and not own(absorbed)):
            return False
        if action == "create":
            if name in known:
                return False
            created.add(name)
            return True
        if own(name) or name in created:
            return True
        if action not in {"delete", "archive"} and shared_editable(name):
            return True
        return False

    def visible(name):
        return name not in retired and (own(name) or name in created or name in published)

    def load_skill(name, file_path=None):
        if own(name) or name in created:
            return None  # Native review may see its own current working copy.
        if name not in published or name in retired:
            return {"success": False, "error": "Skill is unavailable in this workspace"}
        from .skill_reads import load
        try:
            value = load(settings, name, project, file_path, issue_delivery=False)
            return {"success": True, "name": name, "content": value["content"],
                    "version": value["version"], "linked_files": sorted(value["files"])}
        except (ValueError, OSError, PermissionError, RuntimeError):
            return {"success": False, "error": "Published skill is unavailable"}
    return can_edit, visible, load_skill


@contextmanager
def native_scope(settings, project, filters=None):
    from agent.shared_learning_scope import Scope, use
    directory, personal = directories(settings, project)
    user = memory_path(settings, project, "user")
    memory = memory_path(settings, project, "memory") if project else directory / "memories/MEMORY.md"
    if (directory / "pending").is_symlink():
        raise PermissionError("Shared pending directory must not be symlinked")
    can_edit, visible, load_skill = filters or (None, None, None)
    def filter_memory(target, entries):
        with closing(Store(settings.state)) as store:
            return _visible_entries(settings, store, project, target, entries)

    with use(Scope(memory, user, directory / "pending", project,
                   lambda target, before, after: _guard_memory(settings, project, target, before, after),
                   can_edit, visible, load_skill, filter_memory,
                   lambda target, before, after: _commit_memory(settings, project, target, before, after))):
        yield


def _memory_snapshot(settings, store, project):
    return {target: _entries(settings, store, project, target) for target in TARGETS}


def review_scope(bridge, project):
    return native_scope(bridge.settings, project, _skill_filters(bridge, project))


def migrate(bridge, *, dry_run=True):
    """Migrate only exact, previously indexed project entries. Retain original files and queues."""
    from tools.memory_tool import load_on_disk_store
    settings, store = bridge.settings, bridge.store
    origins = store.get("knowledge-index-v1", {}).get("memory", {})
    plan = []
    for key, filename, text in memory_entries(bridge.api.read(kind="context")):
        project = origins.get(key, {}).get("project")
        state = "unassigned"
        if filename == "MEMORY.md" and project and settings.project_for(project) == project:
            if store.get("shared-memory-migrated:" + key):
                state = "already_migrated"
            elif not dry_run:
                with native_scope(settings, project):
                    result = load_on_disk_store().add("memory", text)
                state = "migrated" if result["success"] else "refused"
                if state == "migrated":
                    store.set("shared-memory-migrated:" + key, True)
            else:
                state = "eligible"
        plan.append({"id": key, "target": filename, "project": project, "state": state})
    from .approvals import ID_PATTERN
    from .store import atomic_json
    pending_dir = settings.home / "pending" / "memory"
    for row in store.db.execute("SELECT payload FROM notices WHERE json_extract(payload,'$.kind')='review.finished'"):
        event = json.loads(row[0])
        project = event.get("project")
        if not project or settings.project_for(project) != project:
            continue
        for fact in event.get("staged", ()):
            identifier = fact.get("result", {}).get("pending_id")
            if fact.get("tool") != "memory" or not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
                continue
            source = pending_dir / (identifier + ".json")
            if store.get("shared-pending-migrated:" + identifier) or source.is_symlink() or not source.is_file():
                continue
            record = json.loads(source.read_text())
            if record.get("id") != identifier or record.get("payload", {}).get("target") != "memory":
                continue
            if not dry_run:
                destination = directories(settings, project)[0] / "pending/memory" / source.name
                if not destination.exists():
                    atomic_json(destination, record)
                store.set("shared-pending-migrated:" + identifier, True)
            plan.append({"id": identifier, "project": project, "state": "pending"})
    return plan


def scoped_context(settings, store, snapshot, project, query):
    from .enterprise import scope_view
    snapshot = scope_view(snapshot, settings.data_policy)
    value = context_view(snapshot, project, query)
    revoked = {row[0] for row in store.db.execute("SELECT name FROM shared_skill_removals")}
    aliases = store.get("publication-aliases", {})
    def allowed(name):
        return name not in revoked and aliases.get(name) not in revoked
    value["skills"] = [item for item in value["skills"] if allowed(item["name"])]
    value["recommended_skills"] = [name for name in value["recommended_skills"] if allowed(name)]
    value.update(memory_view(settings, store, project))
    value["version"] = digest({key: val for key, val in value.items() if key != "version"})
    value["summary"] = "\n".join(value["memories"].values())[:340]
    return value


def _snapshot(settings):
    path = settings.state / "context.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def _pending(settings, project, args):
    from .enterprise import MODE, boundary, scrub
    from tools import write_approval as wa

    subsystem = args.get("subsystem", "memory")
    if subsystem not in {wa.MEMORY, wa.SKILLS}:
        raise ValueError("Invalid pending subsystem")
    enterprise_mode = settings.data_policy.get("mode") == MODE
    if enterprise_mode and not boundary(settings.data_policy):
        return {"subsystem": subsystem, "scope": project or "user", "records": [],
                "reason": "enterprise_boundary_unavailable_or_changed"}
    with native_scope(settings, project):
        records = wa.list_pending(subsystem)
    sanitize = scrub if enterprise_mode else redact
    return {"subsystem": subsystem, "scope": project or "user",
            "records": [{**sanitize(record), "revision": digest(record)} for record in records]}


def approval_list(settings, store, project, identifier=None):
    from collections import Counter
    from .approvals import STATES, change_fingerprint, inspect_record
    from .enterprise import MODE, boundary, scrub
    from .shared_learning import status
    from tools import write_approval as wa
    from tools.memory_tool_store import _patch_entry

    enterprise_mode = settings.data_policy.get("mode") == MODE
    if enterprise_mode and not boundary(settings.data_policy):
        return {"project": project, "proposals": [], "review_revision": digest([]),
                "counts": {state: 0 for state in STATES}, "decisions": [],
                "automatic_approval": False, "state": "withheld",
                "reason": "enterprise_boundary_unavailable_or_changed",
                "shared_learning": status(store, project, settings=settings)}
    with native_scope(settings, project):
        records = wa.list_pending("memory")
    targets = _memory_snapshot(settings, store, project)
    sanitize = scrub if enterprise_mode else redact
    proposals, seen = [], {}
    for record in records:
        target = record["payload"].get("target")
        item = inspect_record(record, targets.get(target, []), _patch_entry, allow_user=True)
        variant, duplicate = item["state"], None
        if variant == "pending":
            stamp = digest([target, change_fingerprint(item["changes"])])
            duplicate = seen.get(stamp)
            if duplicate:
                variant = "duplicate"
            else:
                seen[stamp] = record["id"]
        safe_changes = sanitize(item["changes"])
        proposals.append({"id": record["id"], "project": project, "target": target,
                          "revision": digest(record), "created_at": record.get("created_at"), "state": variant,
                          "summary": sanitize(record.get("summary") or ""), "reason": item["reason"],
                          "operations": safe_changes, "duplicate_of": duplicate,
                          "can_approve": variant == "pending" and safe_changes == item["changes"], "can_reject": True})
    counts = Counter(item["state"] for item in proposals)
    history = []
    for row in store.db.execute("SELECT id,state,payload,created FROM shared_writes WHERE project IS ? "
                                "AND json_extract(payload,'$.operation')='decide' ORDER BY rowid DESC LIMIT 20", (project,)):
        args = json.loads(row["payload"])["args"]
        history.append({"id": row["id"], "pending_id": args.get("id"), "at": row["created"],
                        "state": "approved" if row["state"] == "applied" else row["state"]})
    return {"project": project, "proposals": [p for p in proposals if identifier is None or p["id"] == identifier],
            "review_revision": digest([(p["id"], p["revision"], p["state"]) for p in proposals]),
            "counts": {state: counts[state] for state in STATES}, "decisions": list(reversed(history)),
            "automatic_approval": False, "shared_learning": status(store, project, settings=settings)}


def approval_decision(bridge, project, params):
    from .shared_learning import _require_enterprise_boundary, enqueue, receipt
    settings = bridge.settings
    _require_enterprise_boundary(settings)
    decision = params["decision"]
    args = {"id": params["id"], "revision": params["revision"], "decision": decision, "subsystem": "memory"}
    if (not project or decision not in {"approve", "reject"}):
        raise ValueError("A project and an explicit approval decision are required")
    request_id = digest([project, args])
    with closing(Store(settings.state)) as store:
        value = receipt(store, project, request_id)
        if value["state"] == "unknown":
            proposals = approval_list(settings, store, project, params["id"])["proposals"]
            if len(proposals) != 1 or proposals[0]["revision"] != params["revision"]:
                raise ValueError("Pending proposal changed; refresh the approval page")
            if decision == "approve" and not proposals[0]["can_approve"]:
                raise ValueError("The proposal is not currently eligible for approval")
            enqueue(store, project, {"operation": "decide", "request_id": request_id, "args": args,
                                     "surface": "codex_approval"}, executor="Codex App", settings=settings)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            value = receipt(store, project, request_id)
            if value["state"] not in {"queued", "applying"}:
                break
            time.sleep(0.08)
    result = value.get("result") or {}
    return {**result, "request_id": request_id, "pending_id": args["id"], "applied": value["state"] == "applied",
            "state": "approved" if value["state"] == "applied" else value["state"]}


def _journey(bridge, store, project, args):
    from agent.learning_graph import build_learning_graph

    value = scoped_context(bridge.settings, store, _snapshot(bridge.settings), project, "")
    skills = {row["name"]: row for row in value["skills"]}
    with native_scope(bridge.settings, project, _skill_filters(bridge, project, store=store)):
        graph = build_learning_graph()
    for node in graph["nodes"]:
        if node["kind"] == "skill" and node["id"] in skills:
            node["version"] = skills[node["id"]].get("artifact_version")
            node["scope"] = skills[node["id"]].get("scope")
        if node["kind"] == "memory":
            target = "user" if node.get("memorySource") == "profile" else "memory"
            node["scope"] = "user" if target == "user" else project
            node["version"] = value["memory_versions"].get(target)
    graph["nodes"] = [node for node in graph["nodes"] if node.get("version")]
    ids = {node["id"] for node in graph["nodes"]}
    graph["edges"] = [edge for edge in graph["edges"] if edge["source"] in ids and edge["target"] in ids]
    from agent.learning_graph import memory_node_id
    graph["memory"] = [card for i, card in enumerate(graph["memory"]) if memory_node_id(card, i) in ids]
    # The installed Desktop sends the selected node ID unchanged on edit/delete.
    # Carry the snapshot version in that identity so its original form stays safe.
    versions = {node["id"]: f"shared:{node['version']}:{node['id']}" for node in graph["nodes"]}
    if args.get("id"):
        requested = args["id"]
        if requested in skills:
            version = skills[requested].get("artifact_version")
            if version:
                from .skill_reads import load
                body = load(bridge.settings, requested, project, artifact_version=version, issue_delivery=False)
                return {"ok": True, "kind": "skill", "label": requested, "content": body["content"],
                        "version": version, "scope": skills[requested].get("scope"),
                        "id": f"shared:{version}:{requested}"}
        node = next((node for node in graph["nodes"] if versions[node["id"]] == requested), None)
        if node is None:
            return {"ok": False, "message": "Refresh Journey to obtain the current node for this workspace"}
        node_id = node["id"]
        if node["kind"] == "skill":
            from .skill_reads import load
            body = load(bridge.settings, node_id, project, artifact_version=node["version"], issue_delivery=False)
            return {"ok": True, "content": body["content"], "kind": "skill", "label": node["label"],
                    "id": args["id"], "version": node["version"], "scope": node["scope"]}
        from agent.learning_mutations import node_detail
        with native_scope(bridge.settings, project):
            item = node_detail(node_id)
        return {**item, "id": args["id"], "version": node["version"], "scope": node["scope"]}
    for node in graph["nodes"]:
        node["id"] = versions[node["id"]]
    for edge in graph["edges"]:
        edge["source"] = versions[edge["source"]]
        edge["target"] = versions[edge["target"]]
    return {**graph, "scope": project or "user"}


def _history(bridge, store, project, args):
    """Select scoped admitted sessions before opening messages through native SessionDB."""
    from .history_reads import search_history
    return search_history(
        bridge.settings, store, project,
        query=args.get("query", ""), session_id=args.get("session_id"),
        limit=args.get("limit") or 10, role_filter=args.get("role_filter") or "user,assistant",
    )


def read(bridge, store, kind, project, args):
    from .shared_learning import _require_enterprise_boundary
    settings = bridge.settings
    if kind == "view":
        return scoped_context(settings, store, _snapshot(settings), project, args.get("query", ""))
    if kind == "skill":
        _require_enterprise_boundary(settings)
        from .skill_reads import load
        return load(settings, args["name"], project, args.get("file_path"),
                    artifact_version=args.get("artifact_version"), expose_path=False)
    if kind == "pending":
        return _pending(settings, project, args)
    if kind == "journey":
        return _journey(bridge, store, project, args)
    if kind == "history":
        return _history(bridge, store, project, args)
    if kind == "archived":
        name = args["name"]
        origin = store.get("archived-skill-origins", {}).get(name, {}).get("origin", {})
        row = store.db.execute("SELECT version FROM shared_skill_removals WHERE name=?", (name,)).fetchone()
        return {"version": row[0]} if project and row and origin.get("projects") == [project] else {}
    if kind == "feedback":
        from .feedback import record
        from .shared_learning import _id, chat_id
        if project is None:
            raise PermissionError("A workspace is required for skill feedback")
        chat, turn = chat_id(settings, args["session"]), _id(args["turn"])
        tests = [{"chat": chat, "turn": turn, "tool_call": _id(call)} for call in args.get("tests", ())]
        return record(store, args["delivery_token"], project, args["stage"], tests)
    raise ValueError("Unknown shared learning read")


def _memory_args(args):
    fields = {"target", "action", "content", "old_text", "new_text", "operations"}
    if set(args) - fields or args.get("target", "memory") not in TARGETS:
        raise ValueError("Invalid native memory operation")
    return args


def _skills_args(args):
    fields = {"action", "name", "content", "category", "file_path", "file_content", "old_string",
              "new_string", "replace_all", "absorbed_into", "operations"}
    if set(args) - fields:
        raise ValueError("Invalid native skill operation")
    return args


def _clear_memory(memory, target, base):
    from tools.memory_tool import _memory_target_error

    if error := _memory_target_error(memory, target):
        return error

    def clear_entries(entries, _limit):
        version = digest(entries)
        if version != base:
            return {"success": False, "conflict": True, "version": version,
                    "error": "Shared memory changed; refresh its version"}
        if not entries:
            return {"success": True}
        return [], "Cleared the confirmed memory scope."

    # Reuse native locking, atomic writes and removal intents. Ordinary consolidation
    # batches continue to reject removing every entry.
    return memory._mutate(target, clear_entries)


def _apply_memory(bridge, project, operation, args, base):
    from tools.memory_tool import load_on_disk_store, memory_tool
    settings, store = bridge.settings, bridge.store
    target = args.get("target", "memory")
    if target not in TARGETS or (target == "memory" and project is None):
        raise PermissionError("Select a valid memory target and workspace")
    before = _memory_snapshot(settings, store, project)
    current = digest(before[target])
    if base != current:
        return "conflict", {"version": current, "error": "Shared memory changed; refresh its version"}
    with native_scope(settings, project):
        memory = load_on_disk_store()
        if operation == "clear":
            if args.get("confirmed") is not True:
                raise PermissionError("Confirm the exact scope to clear")
            result = _clear_memory(memory, target, base)
            if result.get("conflict"):
                return "conflict", {"version": result["version"], "error": result["error"]}
        else:
            result = json.loads(memory_tool(store=memory, **_memory_args(args)))
    after = _memory_snapshot(settings, store, project)
    if result.get("staged"):
        from tools import write_approval
        with native_scope(settings, project):
            if not write_approval.get_pending("memory", result["pending_id"]):
                return "failed", {"error": "The native pending write was not persisted"}
    return _tool_state(result), {**result, "version": digest(after[target]), "scope": target}


def _tool_state(result):
    if not result.get("success", result.get("ok", False)):
        return "failed"
    return "pending_review" if result.get("staged") else "applied"


def _retire(bridge, name, version, origin):
    store = bridge.store
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO shared_skill_removals VALUES (?,?,?)",
                         (name, version or "", time.time()))
        archived = store.get("archived-skill-origins", {})
        archived[name] = {"origin": origin, "version": version,
                          "attestation": store.get("semantic-skill-attestations", {}).get(name, {})}
        store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("archived-skill-origins", dumps(archived)))
        for key in ("publications-v1", "publication-reviews-v1", "artifact-read-cache", "publication-decisions"):
            value = store.get(key, {})
            value.pop(name, None)
            store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dumps(value)))


def _apply_skill(bridge, project, operation, args, base):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_usage import archive_skill, restore_skill, set_locked

    name = args.get("name")
    if not isinstance(name, str) or name != Path(name).name or not name:
        raise ValueError("Use the canonical skill name")
    store = bridge.store
    origin = store.get("knowledge-index-v1", {}).get("skills", {}).get(name, {})
    filters = _skill_filters(bridge, project)
    can_edit, _, _ = filters
    action = args.get("action")
    if operation == "restore":
        origin = store.get("archived-skill-origins", {}).get(name, {}).get("origin", {})
        removed = store.db.execute("SELECT version FROM shared_skill_removals WHERE name=?", (name,)).fetchone()
        if not project or origin.get("projects") != [project] or not removed or base != removed[0]:
            return "conflict", {"error": "An exact archived version in the current project is required"}
        ok, message = restore_skill(name)
        if ok:
            with store.db:
                store.db.execute("DELETE FROM shared_skill_removals WHERE name=?", (name,))
                index = store.get("knowledge-index-v1", {"memory": {}, "skills": {}})
                index["skills"][name] = origin
                store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("knowledge-index-v1", dumps(index)))
        return ("applied" if ok else "failed"), {"success": ok, "message": message, "version": _version(bridge, name)}
    if project is None or (action != "create" and not can_edit(action, name, args.get("absorbed_into"))):
        raise PermissionError("Only the originating project can edit this shared skill")
    current = _version(bridge, name)
    if base != current or (action == "create" and current is not None):
        return "conflict", {"version": current, "error": "Shared skill changed; refresh its version"}
    if args.get("operations") is not None:
        operations = args["operations"]
        if not isinstance(operations, list) or any(not isinstance(op, dict) or op.get("name", name) != name for op in operations):
            raise ValueError("A versioned skill write can update one skill package")
    with native_scope(bridge.settings, project, filters):
        if action == "archive":
            ok, message = archive_skill(name)
            result = {"success": ok, "message": message}
        elif action in {"lock", "unlock"}:
            ok = set_locked(name, action == "lock")
            result = {"success": ok, "locked": action == "lock"}
        else:
            result = json.loads(skill_manage(**_skills_args(args)))
    if result.get("staged"):
        bind_skill_proposal(bridge, project, result["pending_id"], {name: current})
    elif result.get("success"):
        bridge.update_knowledge(bridge.api.read(kind="context"), project=project,
                                result={"applied": [{"tool": "skill_manage", "operation": args, "result": result}]})
        if action in {"delete", "archive"}:
            _retire(bridge, name, current, origin)
    return _tool_state(result), {**result, "version": _version(bridge, name)}


def bind_skill_proposal(bridge, project, identifier, versions):
    from tools import write_approval as wa
    from .approvals import ID_PATTERN
    if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
        return
    with native_scope(bridge.settings, project):
        record = wa.get_pending("skills", identifier)
    if record is None:
        return
    payload = record["payload"]
    ops = payload.get("operations") or [payload]
    names = {op.get("name") or payload.get("name") for op in ops}
    if all(isinstance(name, str) for name in names):
        bridge.store.set("shared-skill-pending:" + identifier, {
            "project": project, "revision": digest(record),
            "versions": {name: versions.get(name) for name in names},
        })


def _decide(bridge, project, args):
    from .approvals import ID_PATTERN
    from tools import write_approval as wa
    from tools.memory_tool import apply_memory_pending, load_on_disk_store
    from tools.skill_manager_tool import apply_skill_pending

    subsystem, identifier, decision = args["subsystem"], args["id"], args["decision"]
    if (subsystem not in {"memory", "skills"} or not isinstance(identifier, str)
            or not ID_PATTERN.fullmatch(identifier) or decision not in {"approve", "reject"}):
        raise ValueError("Invalid pending decision")
    settings, store = bridge.settings, bridge.store
    origins = store.get("knowledge-index-v1", {}).get("skills", {})
    with native_scope(settings, project, _skill_filters(bridge, project)):
        record = wa.get_pending(subsystem, identifier)
        if not record or digest(record) != args.get("revision"):
            return "conflict", {"error": "Pending proposal changed or is outside this scope"}
        if decision == "reject":
            rejected = wa.discard_pending(subsystem, identifier)
            return "rejected" if rejected else "failed", {"success": rejected, "pending_removed": rejected}
        payload = record["payload"]
        if subsystem == "memory":
            result = apply_memory_pending(payload, load_on_disk_store())
        else:
            pinned = store.get("shared-skill-pending:" + identifier, {})
            if (pinned.get("project") != project or pinned.get("revision") != digest(record)
                    or not pinned.get("versions") or any(_version(bridge, name) != version
                                                         for name, version in pinned["versions"].items())):
                return "conflict", {"error": "Skill changed after this proposal; request a new proposal"}
            result = json.loads(apply_skill_pending(payload))
        if result.get("success"):
            result["pending_removed"] = wa.discard_pending(subsystem, identifier)
    if result.get("success") and subsystem == "skills":
        bridge.update_knowledge(bridge.api.read(kind="context"), project=project,
                                result={"applied": [{"tool": "skill_manage", "operation": payload, "result": result}]})
        for op in payload.get("operations") or [payload]:
            if op.get("action") in {"archive", "delete"}:
                name = op.get("name") or payload.get("name")
                _retire(bridge, name, pinned["versions"].get(name), origins.get(name, {}))
    return _tool_state(result), result


def _journey_write(bridge, project, operation, args, base):
    node = args.get("id")
    if not isinstance(node, str):
        raise ValueError("Journey node ID is required")
    marker, _, identity = node.partition(":")
    version, _, node = identity.partition(":")
    if marker != "shared" or not node or not version or version != base:
        return "conflict", {"error": "Refresh Journey to obtain the current version"}
    from agent.learning_mutations import _locate_memory, _parse_memory_id
    edit = operation == "journey_edit"
    if not node.startswith("memory:"):
        return _apply_skill(bridge, project, "skills",
                            {"name": node, "action": "patch", "content": args.get("content")} if edit
                            else {"name": node, "action": "archive"}, base)
    # Reject positional legacy IDs: a list shift must never change the edited entry.
    source, _, fingerprint = _parse_memory_id(node)
    if not fingerprint:
        return "conflict", {"error": "Refresh Journey to obtain a versioned memory node"}
    with native_scope(bridge.settings, project):
        try:
            _, chunks, index = _locate_memory(node)
        except ValueError:
            return "conflict", {"error": "Memory no longer matches Journey"}
        text = chunks[index]
    target = "user" if source == "profile" else "memory"
    return _apply_memory(bridge, project, "memory",
                         {"action": "patch" if edit else "remove", "target": target,
                          "old_text": text, **({"content": args["content"]} if edit else {})}, base)


def _refine(bridge, project, body):
    from .shared_learning import chat_id
    from .learning_requests import rpc as learning_request
    if not project:
        raise PermissionError("A workspace is required for learning")
    chat = chat_id(bridge.settings, body["source"]["chat"])
    params = {"request_id": "hermes-" + digest([project, body]), "chat_id": chat, "mode": "refine",
              "focus": str(body["args"].get("focus", ""))[:4000]}
    result = learning_request(bridge.store, bridge.settings, project, "request_learning", params)
    return ("failed" if result["state"] == "no_evidence" else "review_queued"), result


def apply_write(bridge, project, body):
    from .shared_learning import _require_enterprise_boundary
    _require_enterprise_boundary(bridge.settings)
    operation, args, base = body["operation"], body["args"], body["base"]
    if operation in {"memory", "clear"}:
        return _apply_memory(bridge, project, operation, args, base)
    if operation in {"skills", "restore"}:
        return _apply_skill(bridge, project, operation, args, base)
    if operation == "decide":
        return _decide(bridge, project, args)
    if operation in {"journey_edit", "journey_delete"}:
        return _journey_write(bridge, project, operation, args, base)
    if operation == "refine":
        return _refine(bridge, project, body)
    raise ValueError("Unknown shared write operation")
