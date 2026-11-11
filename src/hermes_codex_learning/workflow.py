"""Closed-vocabulary execution evidence. Never copy task text into this projection."""
from __future__ import annotations

import shlex

from .store import digest, dumps

MODE = 'workflow-v1'
EXECUTOR = 'Codex workflow metadata'
PROJECT = '/codex-workflow'
ACTIONS = {'inspect', 'edit', 'test', 'execute', 'other'}
OUTCOMES = {'running', 'completed', 'failed', 'interrupted', 'unknown'}
USER = 'Observed workflow metadata only; task content and command output were withheld.'
END = 'The observed executor turn ended. Tool outcomes do not prove task correctness.'


def action(payload: dict) -> str:
    name = str(payload.get('tool_name', '')).rsplit('.', 1)[-1].lower()
    if name in {'apply_patch', 'edit', 'write'}:
        return 'edit'
    if name in {'read', 'read_file', 'view_image', 'glob', 'grep'}:
        return 'inspect'
    if name not in {'bash', 'exec_command', 'terminal', 'exec', 'wait', 'write_stdin'}:
        return 'other'
    args = payload.get('tool_input')
    command = (args.get('command') or args.get('cmd')) if isinstance(args, dict) else None
    if isinstance(command, str) and len(command) <= 65536:
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        # Only classify simple commands. Shell compositions carry no test claim.
        if words and not any(c in command for c in ';|&`$\n<>'):
            binary = words[0].rsplit('/', 1)[-1]
            if binary in {'pytest', 'unittest'} or (binary in {'python', 'python3'} and
                    words[1:3] in (['-m', 'pytest'], ['-m', 'unittest'])):
                return 'test'
            if binary in {'rg', 'cat', 'sed', 'head', 'tail', 'ls', 'pwd'}:
                return 'inspect'
    return 'execute'


def project_event(payload: dict) -> dict:
    from .normalize import _result_state
    event = payload.get('hook_event_name')
    result = {key: payload[key] for key in ('hook_event_name', 'session_id', 'turn_id', 'cwd',
              'tool_use_id', 'transcript_path') if key in payload}
    result['bridge_evidence_mode'] = MODE
    if event == 'UserPromptSubmit':
        result['prompt'] = USER
    elif event == 'Stop':
        result['last_assistant_message'] = END
    elif event in {'PreToolUse', 'PostToolUse'}:
        result['tool_name'] = 'workflow_' + action(payload)
        result['tool_input'] = {}
        state = 'running'
        if event == 'PostToolUse':
            name = str(payload.get('tool_name', ''))
            response = payload.get('tool_response')
            # Free-form result text is not an authoritative exit status.
            state = _result_state(name, response)[0] if isinstance(response, dict) else 'unknown'
        result['tool_response'] = {'outcome': state if state in OUTCOMES else 'unknown'}
    return result


def observation(data: dict) -> dict:
    source = data['source']
    steps = []
    for tool in data['tools'].values():
        category = str(tool.get('name', '')).removeprefix('workflow_')
        state = tool.get('status')
        steps.append({'action': category if category in ACTIONS else 'other',
                      'outcome': state if state in OUTCOMES else 'unknown'})
    return {'source': {'host': 'codex', 'project': PROJECT, 'executor': EXECUTOR,
                      'chat': digest([MODE, source['host'], source['project'], source['chat']]),
                      'turn': digest([MODE, source['chat'], source['turn']])},
            'user': USER + '\n' + dumps({'steps': steps[-128:]}), 'assistant': END,
            'tools': [], 'status': 'interrupted' if data['status'] == 'interrupted' else 'completed',
            'coverage': ['workflow_metadata_only']}
