"""Bound the copy of external observations replayed by the native review."""
from __future__ import annotations

import json
import re

from .store import dumps

MAX_HISTORY_BYTES = 64 * 1024
BUDGET_EXHAUSTED = "review_input_budget_exhausted"
_MEDIA_URL = re.compile(r"data:([a-z0-9.+-]+/[a-z0-9.+-]+)(?:;[a-z0-9_.=+%-]+)*;base64,[a-z0-9+/_=%-]+",
                        re.IGNORECASE | re.ASCII)
_OMITTED = {"role": "user", "content": "[Some external execution evidence was omitted to fit the review budget.]"}


def is_pending_tool_reply(error: str) -> bool:
    return "No reply:" in error and "while a tool result was still pending" in error


def _without_media(text: str) -> str:
    text = _MEDIA_URL.sub(lambda match: f"[{match[1]} attachment omitted from review]", text)
    if '"data"' not in text or not text.lstrip().startswith(("{", "[")):
        return text
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return text
    blocks = value.get("content", [value]) if isinstance(value, dict) else value
    if not isinstance(blocks, list):
        return text
    changed = False
    for block in blocks:
        if (isinstance(block, dict) and block.get("type") in ("image", "audio")
                and isinstance(block.get("data"), str)):
            block["data"] = "[Attachment omitted from review]"
            changed = True
    return dumps(value) if changed else text


def _preview(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = "\n[Middle omitted from review]\n"
    keep = (limit - len(marker)) // 2
    return data[:keep].decode("utf-8", "ignore") + marker + data[-keep:].decode("utf-8", "ignore")


def _review_message(message: dict, limit: int | None = None, *, tool_limit: int = 4096) -> dict:
    result = dict(message)
    content = message.get("content")
    if isinstance(content, str):
        field_limit = tool_limit if message["role"] == "tool" else 12 * 1024
        result["content"] = (_without_media(content) if limit is None
                             else _preview(content, min(field_limit, limit // 4)))
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = call["function"]
            arguments = function["arguments"]
            if limit is None:
                arguments = _without_media(arguments)
            else:
                preview = _preview(arguments, min(2 * 1024, limit // 4))
                if preview != arguments:
                    arguments = dumps({"omitted_arguments_preview": preview})
            calls.append({**call, "function": {**function, "arguments": arguments}})
        result["tool_calls"] = calls
    return result


def bound_review_history(messages: list[dict], limit: int = MAX_HISTORY_BYTES) -> list[dict]:
    """Keep recent evidence and the latest user request, with tool pairs intact."""
    if limit < 512:
        return []
    cleaned = [_review_message(message) for message in messages]
    if len(dumps(cleaned).encode("utf-8")) <= limit:
        return cleaned

    # Give many small previews a chance to fit before dropping whole exchanges.
    tool_count = sum(message["role"] == "tool" for message in cleaned)
    tool_limit = min(4096, max(256, limit // (2 * max(1, tool_count))))
    groups: list[list[dict]] = []
    for message in cleaned:
        bounded = _review_message(message, limit, tool_limit=tool_limit)
        if message["role"] == "tool" and groups and groups[-1][0].get("tool_calls"):
            groups[-1].append(bounded)
        else:
            groups.append([bounded])
    sizes = [len(dumps(group).encode("utf-8")) for group in groups]
    if sum(sizes) + 2 <= limit:
        return [message for group in groups for message in group]

    recent = range(len(groups) - 1, -1, -1)
    user_index = next((i for i in recent if groups[i][0]["role"] == "user"), None)
    selected = set()
    remaining = limit - len(dumps([_OMITTED]).encode("utf-8")) - 2
    if user_index is not None and sizes[user_index] <= remaining:
        selected.add(user_index)
        remaining -= sizes[user_index]
    for i in recent:
        if i not in selected and sizes[i] <= remaining:
            selected.add(i)
            remaining -= sizes[i]
    if not selected:
        return []
    return [dict(_OMITTED)] + [message for i in sorted(selected) for message in groups[i]]


def install_review_policy(api) -> None:
    """Adapt the v1 runtime instance from the pinned Learning API patch."""
    from agent.background_review import _review_input_token_budget, load_background_review_settings
    from agent.learning_runtime import NativeLearningRuntime

    # The v1 facade does not expose the replay snapshot. Use the worker's own
    # native runtime instance, after journal admission and before the review fork.
    runtime = getattr(getattr(api, "_service", None), "runtime", None)
    if api.version != 1 or not isinstance(runtime, NativeLearningRuntime):
        raise RuntimeError("Unsupported native learning runtime for bounded review input")
    native_review = runtime.review

    def review(handle, source, messages, intent):
        enabled, task = load_background_review_settings()
        if not enabled:
            return native_review(handle, source, messages, intent)
        parent = runtime.agent(handle, source)
        provider, model = (str(task.get(key, "")).strip() for key in ("provider", "model"))
        routed = provider not in ("", "auto") and model and (provider, model) != (parent.provider, parent.model)
        budget = _review_input_token_budget(task, None if routed else parent)
        # Keep most of the aggregate budget for the system prompt, tool results
        # and follow-up calls. Serialized UTF-8 bytes bound the replay payload.
        limit = MAX_HISTORY_BYTES if budget is None else min(MAX_HISTORY_BYTES, budget // 4)
        bounded = bound_review_history(messages, limit)
        if messages and not bounded:
            return {"run_status": "failed", "failure_reason": BUDGET_EXHAUSTED,
                    "error": "The background review input budget is too small to replay the evidence.",
                    "applied": [], "staged": [], "refused": [], "uncertain": [], "usage": None, "no_change": False}
        outcome = native_review(handle, source, bounded, intent)
        usage = outcome.get("usage") or {}
        if (outcome.get("run_status") == "failed" and is_pending_tool_reply(outcome.get("error") or "")
                and (usage.get("provider"), usage.get("model")) == (parent.provider, parent.model)):
            # The fork may fall back to the parent after a configured route fails.
            actual_budget = _review_input_token_budget(task, parent)
            if actual_budget is not None and (usage.get("input_tokens") or 0) >= actual_budget:
                return {**outcome, "failure_reason": BUDGET_EXHAUSTED}
        return outcome

    runtime.review = review
