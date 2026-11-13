"""Closed-vocabulary execution evidence. Never copy task text into this projection."""
from __future__ import annotations

import re
import shlex
from itertools import dropwhile
from pathlib import Path

from .store import digest, dumps

MODE = 'workflow-v1'
EXECUTOR = 'Codex workflow metadata'
PROJECT = '/codex-workflow'
ACTIONS = {'inspect', 'edit', 'test', 'execute', 'other'}
OUTCOMES = {'running', 'completed', 'failed', 'interrupted', 'unknown'}
USER = 'Observed workflow metadata only; task content and command output were withheld.'
END = 'The observed executor turn ended. Tool outcomes do not prove task correctness.'
_INSPECTION_COMMANDS = {"rg", "cat", "sed", "head", "tail", "ls", "pwd"}
_TEST_SUBCOMMANDS = {"go", "cargo", "swift", "dotnet"}
_PACKAGE_RUNNERS = {"npm", "pnpm", "yarn", "bun"}
_PYTHON_FLAGS = {"-B", "-E", "-I", "-O", "-OO", "-P", "-S", "-q", "-s", "-u"}
_NON_RUNNING_TEST_FLAGS = {"-h", "--help", "-V", "--version", "--collect-only", "--co",
                           "--dry-run", "--list", "--list-tests", "--no-run", "--if-present"}
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")


def _runs_tests(binary: str, arguments: list[str]) -> bool:
    if _NON_RUNNING_TEST_FLAGS.intersection(argument.split("=", 1)[0] for argument in arguments):
        return False
    if binary in {"pytest", "unittest"}:
        return True
    if binary in _TEST_SUBCOMMANDS:
        return arguments[:1] == ["test"]
    if binary in _PACKAGE_RUNNERS:
        return arguments[:1] == ["test"] or arguments[:2] in (["run", "test"], ["run-script", "test"])
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", binary):
        invocation = list(dropwhile(_PYTHON_FLAGS.__contains__, arguments))
        return (invocation[:2] in (["-m", "pytest"], ["-m", "unittest"])
                or bool(invocation and Path(invocation[0]).parts[-2:] == ("scripts", "check_release.py")))
    return False


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
    # Shell compositions can hide the exit status of the actual test command.
    if not isinstance(command, str) or len(command) > 65536 or any(c in command for c in ';|&`$\r\n<>'):
        return 'execute'
    try:
        words = list(dropwhile(_ENV_ASSIGNMENT.match, shlex.split(command)))
    except ValueError:
        return 'execute'
    if words:
        binary = words[0].rsplit('/', 1)[-1]
        if _runs_tests(binary, words[1:]):
            return 'test'
        if binary in _INSPECTION_COMMANDS:
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
            if isinstance(response, dict):
                process = _result_state(name, response)[1]
                if process:
                    result['bridge_process'] = digest([payload.get('session_id'), process])
        args = payload.get('tool_input')
        if isinstance(args, dict):
            polled = args.get('session_id', args.get('cell_id'))
            if isinstance(polled, (str, int)) and not isinstance(polled, bool):
                result['bridge_polled'] = digest([payload.get('session_id'), str(polled)])
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
