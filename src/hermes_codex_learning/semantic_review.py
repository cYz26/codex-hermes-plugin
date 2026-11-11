"""Scope native semantic reviews without replacing native memory/skill writes."""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from .knowledge import memory_entries
from .store import Store, redact
from .workflow import EXECUTOR

PROMPT = """Review only the admitted execution evidence. Do not execute the user's task.
Preserve uncertainty and distinguish execution completion from verified correctness.
Use native memory to add concise project facts, and skill_manage to create or revise
learned-<topic> skills with applicability, prerequisites, limitations and verification.
General procedures may declare learning_scope: global_candidate; private project facts
remain project-specific. Scripts are candidates until independent isolated verification.
Use skill_view before revising a skill; patch supports old_string/new_string or full
content. Write resources using write_file with relative paths. Do not set a category.
skill_manage accepts a single operation or an operations-only array of 1–5 operations;
each operation must include its own action and name.
Learn the change in method, not just the task topic: connect the initial goal,
earlier approach, user addition/correction, and subsequent result using the evidence refs.
Natural additions can reveal an improved workflow without a request to "remember".
Keep project facts in project memory; reusable procedures belong in appropriately
scoped skills. Stable preferences require user evidence and keep their stated scope.
For a stable preference, quote the user's words establishing that ongoing scope.
Every write operation must include evidence: {classification: project_fact|procedure|
stable_preference, basis: [{ref: evidence_ref, quote: verbatim source excerpt}],
rationale: explanation of the supported lesson and this target's relevance}.
Use an own-task quote; parent excerpts provide context only. For a procedure also
cite a result. Quotes must be 8–600 characters; rationale must be 24–2000 characters.
For explicitly admitted material learning, a material ref can support a procedure
directly; reading material never proves that its procedure was executed successfully.
Select a related existing skill by applicability, read it, then revise the supported
step or caution. If none fits, create a new learned-<topic> candidate. Do not append
an unrelated workflow to a skill just because it is published or writable.
Limit a general lesson to the demonstrated conditions, explain source/coverage gaps,
and preserve explicit user constraints. Tool completion is separate from correctness.
Only the memory and skill names supplied below are available. Do not infer hidden context.
Memory revisions can remain pending for human review. Do not resubmit an identical
pending revision; continue with other supported findings instead.
"""


def _denied(message, *, position=None, reason=None):
    if position is not None:
        message = f"第 {position} 项操作：" + message
    result = {"action": "block", "message": message}
    if reason:
        result["reason"] = reason
    return result


class ScopedMemory:
    """Delegate writes to native guards; never echo foreign inventory on errors."""
    def __init__(self, store, entries):
        self._native = store
        self.memory_entries = list(entries)
        self.user_entries = []
        self.memory_enabled, self.user_profile_enabled = True, False
        self.memory_char_limit = getattr(store, "memory_char_limit", 2200)
        self.user_char_limit = getattr(store, "user_char_limit", 1375)
        self._system_prompt_snapshot = {"memory":"\n§\n".join(entries), "user":""}

    def target_enabled(self, target):
        return target == "memory"

    def reset_consolidation_failures(self):
        return self._native.reset_consolidation_failures()

    def get_prompt_block(self, target):
        return self._system_prompt_snapshot.get(target) or None

    def format_for_system_prompt(self, target):
        return self.get_prompt_block(target)

    def load_from_disk(self):
        # Native refresh cannot broaden a review's frozen, assessed context.
        return None

    def _entries_for(self, target):
        return self.memory_entries if target == "memory" else []

    def _usage(self, target):
        return self._native._usage(target)

    @staticmethod
    def safe_result(value):
        if not value.get("success"):
            return {"success":False, "error":"Native memory operation refused; no other project's entries are exposed."}
        return {key:val for key,val in value.items() if key in {"success", "done", "target", "usage", "entry_count"}}

    def add(self, target, content):
        result = self._native.add(target, content)
        if result.get("success") and content not in self.memory_entries:
            self.memory_entries.append(content)
        return self.safe_result(result)

    def apply_batch(self, target, operations):
        if any(op.get("action") != "add" for op in operations):
            return {"success":False, "error":"Consolidation must use the native staged review path"}
        result = self._native.apply_batch(target, operations)
        if result.get("success"):
            for op in operations:
                content = op.get("content") or op.get("new_text")
                if content and content not in self.memory_entries:
                    self.memory_entries.append(content)
        return self.safe_result(result)

    def resolve_entry(self, target, old_text, action):
        result = self._native.resolve_entry(target, old_text, action)
        if result.get("matched_entry") not in self._entries_for(target):
            return {"success":False, "error":"Entry is outside this review's admitted scope"}
        return result

    def resolve_batch_entries(self, target, operations):
        result = self._native.resolve_batch_entries(target, operations)
        if not result.get("success") or any(entry is not None and entry not in self._entries_for(target)
                                           for entry in result.get("matched_entries", [])):
            return {"success":False, "error":"Entries are outside this review's admitted scope"}
        return result


class SemanticGuard:
    def __init__(self, api, settings):
        self.api, self.settings, self.active = api, settings, {}

    def pre_tool(self, *, session_id, tool_name, args, **kwargs):
        scope = self.active.get(session_id)
        if scope is None:
            return None
        if not isinstance(args, dict):
            return _denied("调用参数必须是对象。")
        from .enterprise import MODE, scrub
        filtered=scrub(args) if getattr(self.settings,"data_policy",{}).get("mode")==MODE else redact(args)
        if filtered != args:
            return _denied("参数包含受保护信息；请移除凭据或禁止学习的内容。")
        claims, native_args = [], args
        if tool_name in {"memory","skill_manage"} and "evidence" in scope:
            if tool_name == "skill_manage" and "operations" in args and set(args) != {"operations"}:
                return _denied("技能批量调用只支持 operations；证据放在每项操作内。")
            if tool_name == "memory" and args.get("target","memory") != "memory":
                return _denied("后台复盘只能修改项目记忆（target=memory）。")
            from .review_relevance import check_claim
            operations = args.get("operations") if "operations" in args else [args]
            if not isinstance(operations,list) or not 1 <= len(operations) <= 5:
                return _denied("写入必须包含 1–5 项操作。")
            clean = []
            pending_owned=set(scope["owned"])
            for position, operation in enumerate(operations,1):
                if not isinstance(operation,dict):
                    return _denied("操作必须是对象。",position=position)
                proof = operation.get("evidence")
                native = {key:value for key,value in operation.items() if key != "evidence"}
                if tool_name == "memory" and native.get("target","memory") != "memory":
                    return _denied("后台复盘只能修改项目记忆（target=memory）。",position=position)
                claim, error = check_claim({**scope,"owned":pending_owned},tool_name,native,proof)
                if error:
                    scope["claims"].append({"state":"blocked","tool":tool_name,"name":native.get("name"),"reason":error})
                    return _denied("本次修改缺少有效的任务证据：" + error + "。请引用已提供的原文，并说明适用关系。",position=position,reason=error)
                claims.append({"state":"accepted","tool":tool_name,"name":native.get("name"),"action":native.get("action"),**claim})
                clean.append(native)
                if native.get("action") == "create" and isinstance(native.get("name"),str):
                    pending_owned.add(native["name"])
            native_args = {"operations":clean}
            args = native_args
        if tool_name == "memory":
            if scope.get("review_memory") is False:
                return _denied("本次复盘未选择项目记忆写入；请遵守所选学习范围。")
            if args.get("target", "memory") != "memory":
                return _denied("后台复盘只能修改项目记忆（target=memory）。")
            ops = args.get("operations") or [args]
            if not isinstance(ops, list) or not 1 <= len(ops) <= 5:
                return _denied("记忆批量调用必须包含 1–5 项 operations。")
            for position, op in enumerate(ops, 1):
                if (not isinstance(op, dict) or not isinstance(op.get("action"), str)
                        or op.get("action") not in {"add", "patch", "remove"}):
                    return _denied("记忆操作只支持 add、patch 或 remove。", position=position)
                old = op.get("old_text")
                if op["action"] != "add" and (not isinstance(old, str) or not old
                        or not any(old in entry for entry in scope["memory"])):
                    return _denied("old_text 必须匹配本次复盘已准入的项目记忆。", position=position)
            duplicate = self.pending_match(scope, ops)
            if duplicate:
                return _denied(f"相同记忆修改已在等待批准：{duplicate}，不再创建重复提案。",
                               reason="review_already_pending")
            if claims:
                scope["claims"].extend(claims)
                return {"action":"modify","args":native_args}
            return None
        if tool_name == "skill_view":
            if set(args) - {"name", "file_path"}:
                return _denied("技能读取参数只支持 name 和可选的 file_path。")
            name = args.get("name")
            if not isinstance(name, str) or name not in scope["readable"]:
                return _denied("技能不在本次复盘可读取范围内；请只读取已列出的技能。")
            path = args.get("file_path")
            if path is not None and not self.resource_path(path):
                return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。",
                               reason="review_resource_path")
            scope.setdefault("viewed",set()).add(name)
            return None
        if tool_name != "skill_manage":
            return _denied("后台复盘只能使用 memory、skill_view 和 skill_manage；外部工具不可用。")
        if scope.get("review_skills") is False:
            return _denied("本次复盘未选择技能写入；请遵守所选学习范围。")
        ops = args.get("operations") if "operations" in args else [args]
        if (("operations" in args and set(args) != {"operations"})
                or not isinstance(ops, list) or not 1 <= len(ops) <= 5):
            return _denied("技能批量调用只支持包含 1–5 项的 operations；每项须提供 action 和 name。")
        owned = set(scope["owned"])
        for position, op in enumerate(ops, 1):
            if not isinstance(op, dict):
                return _denied("技能操作参数必须是对象。", position=position)
            name, action = op.get("name"), op.get("action")
            if not isinstance(name, str) or not re.fullmatch(r"learned-[a-z0-9][a-z0-9-]{0,70}", name):
                return _denied("技能名称须为 learned-<topic>，只使用小写字母、数字和连字符。", position=position)
            if action == "create":
                if name in scope["existing"] or (self.settings.home / "skills" / name).exists():
                    return _denied("技能已存在；请先用 skill_view 读取，并通过 patch 修订。", position=position)
                owned.add(name)
            elif not isinstance(action, str) or action not in {"patch", "edit", "write_file"}:
                return _denied("技能操作只支持 create、patch、edit 或 write_file。", position=position)
            elif name not in owned:
                if "evidence" in scope:
                    scope["claims"].append({"state":"blocked","tool":"skill_manage","name":name,"reason":"learning_target_unrelated"})
                return _denied("技能不在本次复盘可修改范围内；请只修改已准入的技能。", position=position)
            elif "evidence" in scope and name in scope["existing"] and name not in scope.get("viewed",set()):
                return _denied("请先读取相关技能的当前版本，再修订本次证据支持的步骤。",position=position,reason="learning_skill_not_viewed")
            if action == "write_file":
                if not self.resource_path(op.get("file_path", "")):
                    return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。",
                                   position=position, reason="review_resource_path")
                if (set(op) - {"name", "action", "file_path", "file_content"}
                        or not isinstance(op.get("file_content"), str)):
                    return _denied("写入参数只支持 name、action、file_path 和字符串 file_content。", position=position)
            elif action == "patch" and "old_string" in op:
                if (set(op) - {"name", "action", "old_string", "new_string", "file_path", "replace_all"}
                        or type(op.get("replace_all", False)) is not bool):
                    return _denied("修订参数只支持 name、action、old_string、new_string、file_path 和布尔 replace_all。", position=position)
                if (not isinstance(op["old_string"], str) or not op["old_string"]
                        or not isinstance(op.get("new_string"), str)):
                    return _denied("修订须提供非空 old_string 和字符串 new_string。", position=position)
                if len(op["old_string"]) + len(op["new_string"]) > 65536:
                    return _denied("单项修订内容超过 65,536 字符；请缩小每次修订范围。", position=position)
                if op.get("file_path") is not None and not self.resource_path(op["file_path"]):
                    return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。",
                                   position=position, reason="review_resource_path")
            elif set(op) - {"name","action","content"} or not isinstance(op.get("content"), str):
                return _denied("全文写入参数只支持 name、action 和字符串 content。", position=position)
            if len(str(op.get("content") or op.get("file_content") or "")) > 65536:
                return _denied("单项写入内容超过 65,536 字符；请缩小每次写入范围。", position=position)
        scope["owned"] = owned
        scope["readable"].update(owned)
        if claims:
            scope["claims"].extend(claims)
            return {"action":"modify","args":native_args}
        return None

    @staticmethod
    def pending_match(scope, operations):
        if not scope.get("pending"):
            return None
        from .approvals import change_fingerprint, inspect_record
        pinned = []
        for op in operations:
            if op["action"] == "add":
                pinned.append(op)
                continue
            matches = [entry for entry in scope["memory"] if op.get("old_text", "") in entry]
            if len(matches) != 1:
                return None
            pinned.append({**op, "matched_entry": matches[0]})
        item = inspect_record({"payload": {"action": "batch", "target": "memory", "operations": pinned}}, scope["memory"])
        return scope["pending"].get(change_fingerprint(item["changes"])) if item["state"] == "pending" else None

    @staticmethod
    def resource_path(value):
        if not isinstance(value,str):
            return False
        path = Path(value)
        return not path.is_absolute() and ".." not in path.parts and path.suffix in {".py",".sh",".md",".txt",".json",".csv"}

    @contextmanager
    def scope(self, handle, messages=None):
        context = self.api.read(kind="context")
        store = Store(self.settings.state)
        try:
            source = store.get("scope:" + handle, {})
            project = source.get("project")
            assessment = store.get("assessment:" + handle,{})
            index = store.get("knowledge-index-v1", {})
            memory_proofs = store.get("semantic-memory-attestations", {})
            skills = store.get("semantic-skill-attestations", {})
            publications = store.get("publications-v1", {})
            pending = []
            if project:
                try:
                    from .approvals import PendingReviews
                    pending = PendingReviews(self.settings, store).list(project)["proposals"]
                except (OSError, ValueError, RuntimeError):
                    # A missing old native client does not broaden review authority.
                    pass
        finally:
            store.close()
        classification = assessment.get("classification") or (
            "synthetic" if assessment.get("basis") == "synthetic_acceptance" else "approved")
        public_review = classification in {"public","synthetic"}
        from .enterprise import boundary
        approved=boundary(self.settings.data_policy)
        enterprise_review=bool(approved and assessment.get("boundary_id")==approved["id"])
        def admissible(proof):
            if proof.get("boundary_id"):
                return bool(enterprise_review and proof["boundary_id"]==approved["id"])
            return (proof.get("classification") in {"public","synthetic"} or
                    (not public_review and proof.get("policy_version") and
                     proof.get("policy_version") == assessment.get("policy_version")))
        entries = [text for key, target, text in memory_entries(context) if target == "MEMORY.md"
                   and index.get("memory", {}).get(key, {}).get("project") == project
                   and memory_proofs.get(key, {}).get("project") == project
                   and admissible(memory_proofs.get(key,{}))]
        versions = {item["name"]:item.get("version") for item in context.get("skills", [])}
        def publication_allowed(name):
            check=publications.get(name,{}).get("check",{})
            return check.get("audience")!="enterprise" or (enterprise_review and check.get("boundary_id")==approved["id"])
        owned = {name for name in versions if index.get("skills", {}).get(name, {}).get("projects") == [project]
                 and skills.get(name, {}).get("catalog_version") == versions[name] and admissible(skills.get(name,{}))
                 and publication_allowed(name)}
        shared = {name for name in versions if publications.get(name, {}).get("catalog_entry", {}).get("version") == versions[name]
                  and (publications.get(name,{}).get("check",{}).get("audience")!="enterprise" or
                       (enterprise_review and publications[name]["check"].get("boundary_id")==approved["id"]))}
        if public_review or enterprise_review:
            owned |= {name for name in shared if name.startswith("learned-")}
        scope = {"owned":owned, "readable":owned | shared, "existing":set(versions), "memory":entries}
        candidate_details = {}
        if messages is not None:
            from .review_relevance import task_evidence, related_skills
            evidence, topic = task_evidence(messages)
            candidate_details = related_skills(context.get("skills",[]),topic,scope["readable"])
            explicit = next((message.get("learning_context",{}) for message in messages if message.get("review_manifest")),{})
            if explicit.get("mode") == "learn" and explicit.get("basis") == "admitted_explicit_material_request":
                target = explicit.get("target")
                if target in scope["readable"]:
                    candidate_details[target] = ["admitted_explicit_target"]
                from .review_relevance import concepts
                topic.update(concepts(str(target).replace("-"," ")))
            scope.update(owned=owned & set(candidate_details), readable=set(candidate_details),
                         evidence=evidence,topic=topic,target_topics=candidate_details,viewed=set(),claims=[])
        from .approvals import change_fingerprint
        scope["pending"] = {change_fingerprint(item["operations"]): item["duplicate_of"] or item["id"]
                            for item in pending if item["state"] in {"pending", "duplicate"}}
        self.active[handle] = scope
        try:
            audience = "\nThis review is for the approved enterprise deployment. Generic skills may be reused inside this enterprise library; they are not public material. Never retain credentials, raw chat logs or unrelated personal details.\n" if enterprise_review else ""
            from .store import dumps
            details = [{"name":item["name"],"description":item.get("description",""),
                        "matching_concepts":candidate_details[item["name"]]}
                       for item in context.get("skills",[]) if item["name"] in candidate_details]
            yield PROMPT + audience + "\nProject memory:\n" + "\n".join(entries) + "\nAvailable skills: " + ", ".join(sorted(scope["readable"])) + "\nTask-relevant candidates: " + dumps(details), entries
        finally:
            self.active.pop(handle, None)


def restrict(agent, prompt, entries, *, require_evidence=False):
    agent._memory_manager = None
    if agent._memory_store is not None and not isinstance(agent._memory_store, ScopedMemory):
        agent._memory_store = ScopedMemory(agent._memory_store, entries)
    agent._user_profile_enabled = False
    agent.enabled_toolsets = ["memory", "skills"]
    agent.tools = [tool for tool in (agent.tools or []) if tool.get("function", {}).get("name") in {"memory", "skill_manage", "skill_view"}]
    if require_evidence:
        from .review_relevance import evidence_tools
        agent.tools = evidence_tools(agent.tools)
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    agent._cached_system_prompt = prompt
    agent._build_system_prompt = lambda *args, **kwargs: prompt


def install_semantic_policy(api, settings, ctx):
    if not settings.data_policy:
        return None
    runtime = api._service.runtime
    original_agent, original_review = runtime.agent, runtime.review
    guard = SemanticGuard(api, settings)
    registration = ctx.register_hook("pre_tool_call", guard.pre_tool)
    def agent(handle, source):
        parent = original_agent(handle, source)
        if source.get("executor") != EXECUTOR:
            restrict(parent, PROMPT, [])
        return parent
    def review(handle, source, messages, intent):
        if source.get("executor") == EXECUTOR:
            return original_review(handle, source, messages, intent)
        from .enterprise import MODE, scrub, explicitly_restricted, boundary, scope_boundary
        store=Store(settings.state)
        try:
            required=scope_boundary(store,handle)
        finally:
            store.close()
        approved=boundary(settings.data_policy)
        if required and (not approved or required!=approved["id"]):
            return {"run_status":"deferred","reason":"enterprise_scope_not_active","applied":[],"staged":[],"uncertain":[]}
        if settings.data_policy.get("mode")==MODE:
            if not approved:
                return {"run_status":"deferred","reason":"enterprise_boundary_changed","applied":[],"staged":[],"uncertain":[]}
            if explicitly_restricted(messages):
                return {"run_status":"failed","reason":"explicitly_restricted_material","applied":[],"staged":[],"uncertain":[]}
            messages=scrub(messages)
        from agent import background_review
        fork_original = background_review.build_cache_parity_fork
        with guard.scope(handle, messages) as (prompt, entries):
            guard.active[handle].update(review_memory=intent["memory"], review_skills=intent["skills"])
            parent = agent(handle, source)
            if isinstance(parent._memory_store, ScopedMemory):
                parent._memory_store = parent._memory_store._native
            restrict(parent, prompt, entries, require_evidence=True)
            def fork(parent, *args, **kwargs):
                if parent.session_id == handle and kwargs.get("conversation_history") is not None:
                    # A routed native fork may digest again. Claims may cite only
                    # the evidence that this fork actually receives.
                    from .review_relevance import task_evidence
                    delivered, _ = task_evidence(kwargs["conversation_history"])
                    guard.active[handle]["evidence"] = delivered
                result = fork_original(parent, *args, **kwargs)
                if parent.session_id == handle:
                    restrict(result[0], prompt, entries, require_evidence=True)
                return result
            background_review.build_cache_parity_fork = fork
            try:
                result = original_review(handle, source, messages, intent)
                return {**result,"learning_candidates":guard.active[handle]["claims"]}
            finally:
                background_review.build_cache_parity_fork = fork_original
                store = Store(settings.state)
                try:
                    store.set("review-candidates:" + str(intent.get("id",handle)),guard.active[handle]["claims"])
                finally:
                    store.close()
    runtime.agent, runtime.review = agent, review
    return registration
