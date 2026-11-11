"""Tie review receipts to native outcomes and matching operation identities."""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock

from .enterprise import scrub
from .review_input import BUDGET_EXHAUSTED, is_pending_tool_reply

_ACTIONS = {
    "add": "新增", "patch": "修订", "replace": "替换", "remove": "删除",
    "create": "创建", "edit": "重写", "delete": "删除",
    "write_file": "写入文件", "remove_file": "删除文件",
}
_FIELDS = ("action", "target", "name", "file_path", "old_text", "new_text",
           "content", "old_string", "new_string", "file_content")
_IDENTITY_FIELDS = ("action", "target", "name", "file_path")
_REVIEW_END = {"completed": "复盘完成", "failed": "复盘失败", "interrupted": "复盘中断"}


def _write_fields(args, fields=_FIELDS):
    return {key: scrub(args[key]) for key in fields if isinstance(args.get(key), str)}


class ReviewWrites:
    """Observe the native fork only while this worker is running its review."""

    def __init__(self):
        self.lock = Lock()
        self.handle = None
        self.calls = {}

    @contextmanager
    def capture(self, handle):
        with self.lock:
            self.handle = handle
            self.calls = {}
        try:
            yield self.calls
        finally:
            with self.lock:
                self.handle = None
                self.calls = {}

    def record_attempt(self, *, session_id, tool_name, tool_call_id, args, **_):
        """Keep operation identities even when a pre-tool guard blocks execution."""
        self._record(session_id, tool_name, tool_call_id, args, _IDENTITY_FIELDS)

    def record(self, *, session_id, tool_name, tool_call_id, args, result=None, status=None, **_):
        try:
            outcome = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            outcome = None
        applied = (status not in ("blocked", "error", "cancelled") and isinstance(outcome, dict)
                   and outcome.get("success") is True and not outcome.get("staged"))
        self._record(session_id, tool_name, tool_call_id, args, _FIELDS if applied else _IDENTITY_FIELDS)

    def _record(self, session_id, tool_name, tool_call_id, args, fields):
        if tool_name not in {"memory", "skill_manage"} or not tool_call_id or not isinstance(args, dict):
            return
        with self.lock:
            if self.handle is None or session_id != self.handle:
                return
            call = _write_fields(args, fields)
            operations = args.get("operations")
            if isinstance(operations, list) and all(isinstance(op, dict) for op in operations):
                call["operations"] = [_write_fields(op, fields) for op in operations]
            self.calls[tool_name, tool_call_id] = call


def _label(fact, args):
    operation = fact.get("operation")
    op = _write_fields(operation if isinstance(operation, dict) else fact, _IDENTITY_FIELDS)
    args = _write_fields(args, _IDENTITY_FIELDS)
    action = op.get("action") or args.get("action")
    verb = _ACTIONS.get(action, action or "操作")
    if fact.get("tool") == "memory":
        target = op.get("target") or args.get("target") or fact.get("result", {}).get("target")
        name = "用户档案" if target == "user" else "记忆"
    else:
        skill = op.get("name") or args.get("name")
        resource = op.get("file_path") or args.get("file_path")
        name = f"技能 {skill}" if skill else "技能操作（名称未记录）"
        if skill or resource:
            name += f" · {resource or 'SKILL.md'}"
    return f"{verb} · {name}"


def _pending_lines(facts, calls, *, refused=False):
    entries = []
    for fact in facts:
        if not isinstance(fact, dict):
            entries.append(((str(fact),), ""))
            continue
        call = calls.get((fact.get("tool"), fact.get("call_id")), {})
        operations = call.get("operations")
        if not fact.get("operation") and operations:
            labels = tuple(_label({**fact, "operation": op}, call) for op in operations)
        else:
            labels = (_label(fact, call),)
        result = fact.get("result") or {}
        reason = (result.get("error") or result.get("message")) if refused else ""
        if refused and (not isinstance(reason, str) or not reason):
            reason = "原始记录未提供拒绝原因。"
        entries.append((labels, reason))
    groups = Counter(entries).items() if refused else ((entry, 1) for entry in entries)
    lines = []
    for (labels, reason), count in groups:
        suffix = f"（{count} 次尝试）" if count > 1 else ""
        lines.append("• " + "；".join(labels) + suffix)
        if reason:
            lines.append("  原因：" + reason)
    return lines


def _text_changes(op, *, memory=False):
    action = op.get("action")
    if memory:
        old = op.get("old_text") if action in {"patch", "replace", "remove"} else None
        new = (op.get("content") or op.get("new_text")) if action in {"add", "patch", "replace"} else None
    else:
        old = op.get("old_string") if action == "patch" else None
        new = {"patch": op.get("content") or op.get("new_string"), "create": op.get("content"),
               "edit": op.get("content"), "write_file": op.get("file_content")}.get(action)
    lines = []
    if old:
        lines.extend(("匹配的原文", old))
    if new is not None:
        lines.extend(("写入的内容", new or "（空文本）"))
    return lines


def _skill_args(fact, call, index):
    """A successful native skill batch returns one result per op in input order."""
    if not fact.get("operation"):
        return call
    op = fact["operation"]
    ops = call.get("operations", [])
    results = fact.get("result", {}).get("results", [])
    if index >= len(ops) or len(ops) != len(results) or results[index] != op:
        return {}
    args = ops[index]
    if (args.get("action"), args.get("name") or call.get("name"), args.get("file_path")) != (
            op.get("action"), op.get("name"), op.get("file_path")):
        return {}
    return args


def _local_time(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def receipt_text(event, calls, created, *, started_at=None):
    project = Path(event["project"]).name if event.get("project") else "未标注项目"
    when = _local_time(created)
    heading = project if started_at is not None else f"{project}  ·  {when}"
    lines = [heading, event["message"], ""]
    if started_at is not None:
        end_status = _REVIEW_END.get(event.get("run_status"), "结果待确认")
        lines.extend(("复盘过程", f"{_local_time(started_at)}  开始复盘", f"{when}  {end_status}", ""))
    positions = Counter()
    for fact in event.get("applied", []):
        key = (fact.get("tool"), fact.get("call_id"))
        call = calls.get(key, {})
        if key[0] == "memory":
            operations = call.get("operations") or [call]
            for args in operations:
                detail = _text_changes(args, memory=True)
                lines.extend((_label(fact, args), *(detail or ["此回执只记录了操作。"]), ""))
        else:
            args = _skill_args(fact, call, positions[key])
            positions[key] += 1
            detail = _text_changes(args)
            if not detail and (fact.get("operation") or fact).get("action") not in {"remove_file", "delete"}:
                detail = ["此回执只记录了操作。"]
            lines.extend((_label(fact, args), *detail, ""))
    for key, heading in (("staged", "等待批准"), ("refused", "被 Hermes 拒绝"), ("uncertain", "结果待确认")):
        facts = event.get(key, [])
        if facts:
            lines.extend((heading, *_pending_lines(facts, calls, refused=key == "refused"), ""))
    error = str(event.get("error") or "")
    if event.get("failure_reason") == BUDGET_EXHAUSTED:
        error = "本次后台复盘达到输入预算，执行记录已保留。"
    elif is_pending_tool_reply(error):
        error = "后台复盘在收到工具结果后结束，模型未生成最终答复。"
    elif "No reply:" in error:
        error = error.partition(" Send `continue`")[0]
    if error:
        lines.extend(("复盘错误", error, ""))
    return scrub("\n".join(lines)).rstrip() + "\n"
