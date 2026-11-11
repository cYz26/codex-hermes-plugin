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
Only the memory and skill names supplied below are available. Do not infer hidden context.
"""


def _denied(message, *, position=None):
    if position is not None:
        message = f"第 {position} 项操作：" + message
    return {"action": "block", "message": message}


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
            return None
        if tool_name == "skill_view":
            if set(args) - {"name", "file_path"}:
                return _denied("技能读取参数只支持 name 和可选的 file_path。")
            name = args.get("name")
            if not isinstance(name, str) or name not in scope["readable"]:
                return _denied("技能不在本次复盘可读取范围内；请只读取已列出的技能。")
            path = args.get("file_path")
            if path is not None and not self.resource_path(path):
                return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。")
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
                return _denied("技能不在本次复盘可修改范围内；请只修改已准入的技能。", position=position)
            if action == "write_file":
                if not self.resource_path(op.get("file_path", "")):
                    return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。", position=position)
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
                    return _denied("资源必须使用技能目录内的相对路径和支持的扩展名，且不能包含 ..。", position=position)
            elif set(op) - {"name","action","content"} or not isinstance(op.get("content"), str):
                return _denied("全文写入参数只支持 name、action 和字符串 content。", position=position)
            if len(str(op.get("content") or op.get("file_content") or "")) > 65536:
                return _denied("单项写入内容超过 65,536 字符；请缩小每次写入范围。", position=position)
        scope["owned"] = owned
        scope["readable"].update(owned)
        return None

    @staticmethod
    def resource_path(value):
        if not isinstance(value,str):
            return False
        path = Path(value)
        return not path.is_absolute() and ".." not in path.parts and path.suffix in {".py",".sh",".md",".txt",".json",".csv"}

    @contextmanager
    def scope(self, handle):
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
        self.active[handle] = scope
        try:
            audience = "\nThis review is for the approved enterprise deployment. Generic skills may be reused inside this enterprise library; they are not public material. Never retain credentials, raw chat logs or unrelated personal details.\n" if enterprise_review else ""
            yield PROMPT + audience + "\nProject memory:\n" + "\n".join(entries) + "\nAvailable skills: " + ", ".join(sorted(scope["readable"])), entries
        finally:
            self.active.pop(handle, None)


def restrict(agent, prompt, entries):
    agent._memory_manager = None
    if agent._memory_store is not None and not isinstance(agent._memory_store, ScopedMemory):
        agent._memory_store = ScopedMemory(agent._memory_store, entries)
    agent._user_profile_enabled = False
    agent.enabled_toolsets = ["memory", "skills"]
    agent.tools = [tool for tool in (agent.tools or []) if tool.get("function", {}).get("name") in {"memory", "skill_manage", "skill_view"}]
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
        with guard.scope(handle) as (prompt, entries):
            guard.active[handle].update(review_memory=intent["memory"], review_skills=intent["skills"])
            parent = agent(handle, source)
            if isinstance(parent._memory_store, ScopedMemory):
                parent._memory_store = parent._memory_store._native
            restrict(parent, prompt, entries)
            def fork(parent, *args, **kwargs):
                result = fork_original(parent, *args, **kwargs)
                if parent.session_id == handle:
                    restrict(result[0], prompt, entries)
                return result
            background_review.build_cache_parity_fork = fork
            try:
                return original_review(handle, source, messages, intent)
            finally:
                background_review.build_cache_parity_fork = fork_original
    runtime.agent, runtime.review = agent, review
    return registration
