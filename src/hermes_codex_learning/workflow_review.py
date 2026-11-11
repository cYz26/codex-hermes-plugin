"""Restrict native workflow reviews to their own generic skills and fixed evidence.

This adapts the same serialized native review worker; it does not start another
agent loop or expose the main library's memories/catalog to metadata-only reviews.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

from .store import Store, redact
from .workflow import EXECUTOR, PROJECT

PROMPT = '''You review fixed-vocabulary execution metadata observed from Codex.
No task text, code, filenames or command output was admitted. Do not infer them.
Only derive modest generic workflow guidance supported by the action/outcome sequence.
An exit status is not proof of correctness or causation. Unknown is not success.
Use skill_manage(operations=[...]) to save useful reusable guidance, or finish without changes.
Skills must be named workflow-<topic>, with YAML learning_scope: global_candidate.
Include applicability, limits and verification. Use prose only, no resource files or category.
Each operation is create or patch (full content), with name, action and content only.
Only your listed workflow skills may be viewed or revised. Memory and other tools
are unavailable. Never invent project conventions from these observations.
'''


def restrict_agent(agent, prompt):
    agent._memory_store = agent._memory_manager = None
    agent._memory_enabled = agent._user_profile_enabled = False
    agent.enabled_toolsets = ['skills']
    agent.tools = [tool for tool in (agent.tools or [])
                   if tool.get('function', {}).get('name') in {'skill_manage', 'skill_view'}]
    agent.valid_tool_names = {tool['function']['name'] for tool in agent.tools}
    agent._cached_system_prompt = prompt
    # Rebuilds after compaction must retain the same data boundary.
    agent._build_system_prompt = lambda *args, **kwargs: prompt


class WorkflowReviewGuard:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings
        self.active = {}

    def pre_tool(self, *, session_id, tool_name, args, **kwargs):
        scope = self.active.get(session_id)
        if scope is None:
            return None
        blocked = {'action': 'block', 'message': 'Workflow review allows skill_view(name) for owned skills, or '
                   'skill_manage(operations=[{name: workflow-topic, action: create|patch, content: full prose}]). '
                   'Include learning_scope: global_candidate in YAML. No category, resource paths or other tools.'}
        if not isinstance(args, dict):
            return blocked
        if tool_name == 'skill_manage' and set(args) == {'operations'}:
            operations = args['operations']
            if not isinstance(operations, list) or not 1 <= len(operations) <= 5:
                return blocked
            saved = {key: set(value) for key, value in scope.items()}
            for op in operations:
                if not isinstance(op, dict) or 'operations' in op:
                    scope.update(saved)
                    return blocked
                verdict = self.pre_tool(session_id=session_id, tool_name=tool_name, args=op)
                if verdict is not None:
                    scope.update(saved)
                    return verdict
            return None
        name = args.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'workflow-[a-z0-9][a-z0-9-]{0,70}', name):
            return blocked
        if tool_name == 'skill_view':
            return None if name in scope['owned'] and set(args) <= {'name'} else blocked
        if tool_name != 'skill_manage' or set(args) - {'name', 'action', 'content'}:
            return blocked
        content = args.get('content')
        if (not isinstance(content, str) or len(content) > 12000 or redact(content) != content
                or not re.search(r'(?m)^learning_scope:\s*global_candidate\s*$', content)):
            return blocked
        action = args.get('action')
        exists_on_disk = self.settings is not None and (self.settings.home / 'skills' / name).exists()
        if action == 'create' and name not in scope['existing'] and not exists_on_disk:
            scope['owned'].add(name)
            scope['existing'].add(name)
            return None
        if action in {'edit', 'patch'} and name in scope['owned']:
            return None
        return blocked

    @contextmanager
    def scope(self, handle):
        context = self.api.read(kind='context')
        store = Store(self.settings.state)
        try:
            provenance = store.get('knowledge-index-v1', {}).get('skills', {})
            attestations = store.get('workflow-skill-attestations', {})
        finally:
            store.close()
        existing = {skill['name'] for skill in context.get('skills', [])}
        catalog_versions = {skill['name']: skill.get('version') for skill in context.get('skills', [])}
        owned = {name for name in existing if provenance.get(name, {}).get('projects') == [PROJECT]
                 and attestations.get(name, {}).get('catalog_version') == catalog_versions[name]
                 and attestations.get(name, {}).get('version')}
        prompt = PROMPT + '\nExisting workflow skills: ' + ', '.join(sorted(owned))
        self.active[handle] = {'owned': owned, 'existing': existing}
        try:
            yield prompt
        finally:
            self.active.pop(handle, None)


def install_workflow_policy(api, settings, ctx):
    runtime = api._service.runtime
    native_agent, native_review = runtime.agent, runtime.review
    guard = WorkflowReviewGuard(api, settings)
    registration = ctx.register_hook('pre_tool_call', guard.pre_tool)

    def agent(handle, source):
        parent = native_agent(handle, source)
        if source.get('executor') == EXECUTOR:
            restrict_agent(parent, PROMPT)
        return parent

    def review(handle, source, messages, intent):
        if source.get('executor') != EXECUTOR:
            return native_review(handle, source, messages, intent)
        from agent import background_review
        original = background_review.build_cache_parity_fork
        with guard.scope(handle) as prompt:
            def fork(parent, *args, **kwargs):
                result = original(parent, *args, **kwargs)
                if parent.session_id == handle:
                    restrict_agent(result[0], prompt)
                return result
            # The bridge executes reviews and Curator serially. Restore even on failure.
            background_review.build_cache_parity_fork = fork
            try:
                return native_review(handle, source, messages, {**intent, 'memory': False, 'skills': True})
            finally:
                background_review.build_cache_parity_fork = original

    runtime.agent, runtime.review = agent, review
    return registration
