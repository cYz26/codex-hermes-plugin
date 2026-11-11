"""Keep the text of applied review writes tied to their native tool-call IDs."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock

from .review_input import BUDGET_EXHAUSTED, is_pending_tool_reply
from .store import redact

_ACTIONS = {
    "add": "新增", "patch": "修订", "replace": "替换", "remove": "删除",
    "create": "创建", "edit": "重写", "delete": "删除",
    "write_file": "写入文件", "remove_file": "删除文件",
}
_FIELDS = ("action", "target", "name", "file_path", "old_text", "new_text",
           "content", "old_string", "new_string", "file_content")
_REVIEW_END = {"completed": "复盘完成", "failed": "复盘失败", "interrupted": "复盘中断"}


def _write_fields(args):
    return {key: redact(args[key]) for key in _FIELDS if isinstance(args.get(key), str)}


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

    def record(self, *, session_id, tool_name, tool_call_id, args, **_):
        if tool_name not in {"memory", "skill_manage"} or not tool_call_id or not isinstance(args, dict):
            return
        with self.lock:
            if self.handle is None or session_id != self.handle:
                return
            call = _write_fields(args)
            operations = args.get("operations")
            if isinstance(operations, list) and all(isinstance(op, dict) for op in operations):
                call["operations"] = [_write_fields(op) for op in operations]
            self.calls[tool_name, tool_call_id] = call


def _label(fact, args):
    op = fact.get("operation") or fact
    action = op.get("action") or args.get("action")
    verb = _ACTIONS.get(action, action or "变更")
    if fact.get("tool") == "memory":
        target = op.get("target") or args.get("target") or fact.get("result", {}).get("target")
        name = "用户档案" if target == "user" else "记忆"
    else:
        skill = op.get("name") or args.get("name")
        resource = op.get("file_path") or args.get("file_path")
        name = f"技能 {skill} · {resource or 'SKILL.md'}" if skill else "技能操作（目标未记录）"
        if not skill and resource:
            name += f" · {resource}"
    return f"{verb} · {name}"


def _text_changes(op, *, memory=False, proposed=False):
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
        lines.extend(("拟写入的内容" if proposed else "写入的内容", new or "（空文本）"))
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


def _unapplied_details(fact, calls, kind):
    if not isinstance(fact, dict):
        return [f"• {fact}"]
    call = calls.get((fact.get("tool"), fact.get("call_id")), {})
    operations = call.get("operations") or [call or fact.get("operation") or fact]
    result = fact.get("result", {})
    lines = []
    for operation in operations:
        label_fact = {**fact, "operation": operation}
        lines.append("• " + _label(label_fact, operation))
        if kind == "staged":
            lines.extend(_text_changes(operation, memory=fact.get("tool") == "memory", proposed=True))
    if kind == "staged" and result.get("pending_id"):
        lines.append("待审编号：" + str(result["pending_id"]))
    reason = result.get("error") or result.get("message")
    if kind == "refused":
        lines.append("拒绝原因：" + str(reason or "原生结果未提供具体原因"))
    elif kind == "uncertain" and reason:
        lines.append("原生结果：" + str(reason))
    return lines


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
            lines.append(heading)
            if key == "staged":
                lines.append("这些提案尚未生效；请在审批页核对差异后批准或丢弃。")
            for fact in facts:
                lines.extend(_unapplied_details(fact, calls, key))
                lines.append("")
    error = str(event.get("error") or "")
    if event.get("failure_reason") == BUDGET_EXHAUSTED:
        error = "本次后台复盘达到输入预算，执行记录已保留。"
    elif is_pending_tool_reply(error):
        error = "后台复盘在收到工具结果后结束，模型未生成最终答复。"
    elif "No reply:" in error:
        error = error.partition(" Send `continue`")[0]
    if error:
        lines.extend(("复盘错误", error, ""))
    return redact("\n".join(lines)).rstrip() + "\n"
