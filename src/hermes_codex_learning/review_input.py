"""Bound the copy of external observations replayed by the native review."""
from __future__ import annotations

import json
import re
import hashlib

from .store import dumps

MAX_HISTORY_BYTES = 64 * 1024
BUDGET_EXHAUSTED = "review_input_budget_exhausted"
_MEDIA_URL = re.compile(r"data:([a-z0-9.+-]+/[a-z0-9.+-]+)(?:;[a-z0-9_.=+%-]+)*;base64,[a-z0-9+/_=%-]+",
                        re.IGNORECASE | re.ASCII)
_EVIDENCE_LABEL = re.compile(r"\[(?:Evidence |External execution evidence )[^\n]*\]\n")


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
    keep = max(0, (limit - len(marker.encode())) // 2)
    return data[:keep].decode("utf-8", "ignore") + marker + data[-keep:].decode("utf-8", "ignore")


def _salient_preview(text: str, limit: int, anchors: str = "") -> str:
    """Extract source sentences related to adjacent requests; never invent a digest."""
    if len(text.encode()) <= limit:
        return text
    from .knowledge import _tokens
    # Source labels establish provenance, not relevance to the adjacent request.
    terms = _tokens(_EVIDENCE_LABEL.sub("", anchors))
    label = _EVIDENCE_LABEL.match(text)
    body = text[label.end():] if label else text
    sentences = [(match.start(), match.group()) for match in
                 re.finditer(r"[^\n.!?。！？]+(?:[\n.!?。！？]|$)", body)]
    ranked = sorted(sentences, key=lambda item: (-len(terms & _tokens(item[1])), item[0]))
    relevant = next((part for _, part in ranked if terms & _tokens(part)), None)
    if not relevant:
        return _preview(text, limit)
    marker = "\n[Source ranges omitted; verbatim excerpts]\n"
    room = max(0, limit - 2 * len(marker.encode()))
    middle = relevant.encode()[:room // 2].decode("utf-8", "ignore").strip()
    edge = room // 4
    raw = text.encode()
    return raw[:edge].decode("utf-8", "ignore") + marker + middle + marker + raw[-edge:].decode("utf-8", "ignore")


def _review_message(message: dict, limit: int | None = None, *, tool_limit: int = 4096, anchors: str = "") -> dict:
    result = dict(message)
    content = message.get("content")
    if isinstance(content, str):
        field_limit = tool_limit if message["role"] == "tool" else 12 * 1024
        result["content"] = (_without_media(content) if limit is None else
                             _preview(content, min(field_limit, limit // 4)) if message["role"] == "tool" else
                             _salient_preview(content, min(field_limit, limit), anchors))
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


def _groups(messages: list[dict]) -> tuple[list[list[dict]], int]:
    """An exchange is replayable only when every result matches its actual call."""
    groups, invalid, i = [], 0, 0
    while i < len(messages):
        message = messages[i]
        calls = message.get("tool_calls") or []
        if calls:
            end = i + 1
            while end < len(messages) and messages[end]["role"] == "tool":
                end += 1
            results = messages[i + 1:end]
            ids = [call.get("id") for call in calls]
            replies = [reply.get("tool_call_id") for reply in results]
            names = {call.get("id"): call.get("function", {}).get("name") for call in calls}
            paired = (all(ids) and len(set(ids)) == len(ids) and len(set(replies)) == len(replies)
                      and set(ids) == set(replies) and all(not reply.get("tool_name") or
                          reply["tool_name"] == names.get(reply.get("tool_call_id")) for reply in results))
            if paired:
                by_id = {reply["tool_call_id"]: reply for reply in results}
                groups.append([message, *[by_id[identifier] for identifier in ids]])
            else:
                invalid += 1
                groups.append([{**{k:v for k,v in message.items() if k != "tool_calls"}, "role":"user",
                    "content":"[Unmatched or incomplete external tool exchange; execution outcome is not verified.]\n"
                              + dumps({"calls":calls,"results":results}), "review_context":True}])
            i = end
        elif message["role"] == "tool":
            invalid += 1
            groups.append([{**message, "role":"user", "review_context":True,
                            "content":"[Unmatched external tool result; no call is inferred.]\n" + str(message.get("content", ""))}])
            i += 1
        else:
            groups.append([message])
            i += 1
    return groups, invalid


def _ranges(indices):
    result = []
    for index in indices:
        if result and result[-1][1] + 1 == index:
            result[-1][1] = index
        else:
            result.append([index, index])
    return result


def bound_review_history_with_report(messages: list[dict], limit: int = MAX_HISTORY_BYTES) -> tuple[list[dict], dict]:
    report = {"original_messages":len(messages), "input_bytes":len(dumps(messages).encode()), "limit_bytes":limit}
    if limit < 512:
        return [], {**report, "reason":BUDGET_EXHAUSTED, "retained_messages":0}
    cleaned = [_review_message(message) for message in messages]
    groups, invalid = _groups(cleaned)
    report["unmatched_exchanges"] = invalid
    flat = [message for group in groups for message in group]
    if len(dumps(flat).encode()) <= limit:
        return flat, {**report, "retained_messages":len(flat), "retained_bytes":len(dumps(flat).encode()),
                      "omitted_group_ranges":[], "clipped_refs":[]}

    dialogue = [i for i,group in enumerate(groups) if not group[0].get("tool_calls")
                and not group[0].get("review_context") and group[0]["role"] in {"user","assistant"}]
    tool_count = sum(len(group) - 1 for group in groups if group[0].get("tool_calls"))
    tool_limit = min(4096, max(256, limit // (2 * max(1, tool_count))))
    duplicates = {}
    bounded = []
    for index, group in enumerate(groups):
        adjacent = " ".join(str(groups[j][0].get("content") or "") for j in dialogue
                            if groups[j][0]["role"] == "user" and
                            (j < index and not any(k in dialogue and groups[k][0]["role"] == "user" for k in range(j+1,index))
                             or j > index and not any(k in dialogue and groups[k][0]["role"] == "user" for k in range(index+1,j))))
        copy = [_review_message(message, min(4096, limit // 4), tool_limit=tool_limit, anchors=adjacent) for message in group]
        for original, preview in zip(group, copy):
            if original["role"] == "tool" and len(str(original.get("content", "")).encode()) > tool_limit:
                checksum = hashlib.sha256(str(original["content"]).encode()).hexdigest()
                if checksum in duplicates:
                    preview["content"] = f"[Repeated external output omitted; sha256={checksum}; first group={duplicates[checksum]}.]"
                else:
                    duplicates[checksum] = index
        bounded.append(copy)

    def pack(selected):
        omitted = _ranges(i for i in range(len(groups)) if i not in selected)
        clipped = [groups[i][0].get("evidence_ref") or groups[i][0].get("message_uid") or f"group:{i}"
                   for i in sorted(selected) if dumps(groups[i]) != dumps(bounded[i])]
        notice = {"role":"user", "observed":True, "review_context":True,
                  "content":"[External evidence omitted to fit the review budget; excerpts are traceable, not saved full history.]\n"
                            + dumps({"omitted_group_ranges":omitted[:8],"additional_omitted_ranges":max(0,len(omitted)-8),
                                     "clipped_groups":len(clipped),"unmatched_exchanges":invalid})}
        return [notice, *[message for i in sorted(selected) for message in bounded[i]]], omitted, clipped

    # Dialogue is the causal spine: goal, additions, prior approach and changed result.
    # Tool volume never determines which user requests get first claim on the budget.
    selected = set(dialogue)
    selected.update(i for i,group in enumerate(groups) if group[0].get("review_manifest"))
    result, omitted, clipped = pack(selected)
    cap = min(4096, limit // 4)
    while len(dumps(result).encode()) > limit and cap > 128:
        cap = max(128, cap // 2)
        for i in dialogue:
            anchors = " ".join(str(groups[j][0].get("content") or "") for j in dialogue
                               if groups[j][0]["role"] == "user" and abs(dialogue.index(j)-dialogue.index(i)) <= 2)
            bounded[i] = [_review_message(groups[i][0], cap, anchors=anchors)]
        result, omitted, clipped = pack(selected)
    if len(dumps(result).encode()) > limit:
        users = [i for i in dialogue if groups[i][0]["role"] == "user"]
        priorities = list(dict.fromkeys(([users[0]] if users else []) + list(reversed(dialogue))))
        selected = {i for i,group in enumerate(groups) if group[0].get("review_manifest")}
        for i in priorities:
            candidate, _, _ = pack(selected | {i})
            if len(dumps(candidate).encode()) <= limit:
                selected.add(i)
    for i in reversed(range(len(groups))):
        if i not in selected:
            candidate, _, _ = pack(selected | {i})
            if len(dumps(candidate).encode()) <= limit:
                selected.add(i)
    result, omitted, clipped = pack(selected)
    if len(dumps(result).encode()) > limit or not selected:
        return [], {**report, "reason":BUDGET_EXHAUSTED, "retained_messages":0}
    return result, {**report, "retained_messages":len(result), "retained_bytes":len(dumps(result).encode()),
                    "omitted_group_ranges":omitted, "clipped_refs":clipped}


def bound_review_history(messages: list[dict], limit: int = MAX_HISTORY_BYTES) -> list[dict]:
    return bound_review_history_with_report(messages, limit)[0]


def causal_digest(messages, original_digest, limit=MAX_HISTORY_BYTES):
    """Let the native digest select its tail, preserving the already bounded dialogue."""
    native = original_digest(messages)
    if native == messages:
        return native, {"used":False}
    tail_ids = {id(message) for message in native[1:]}
    groups, _ = _groups(messages)
    keep, omitted = [], []
    for index,group in enumerate(groups):
        dialogue = any(message.get("evidence_kind") in {"request","result","material"}
                       or message.get("review_manifest") for message in group)
        if dialogue or any(id(message) in tail_ids for message in group):
            keep.extend(group)
        else:
            omitted.append(index)
    notice = {"role":"user","observed":True,"review_context":True,
              "content":"[Native routed review: older tool payloads condensed; source-bearing goal, additions and conclusions retained verbatim.]\n"
                        + dumps({"omitted_tool_group_ranges":_ranges(omitted)})}
    bounded, report = bound_review_history_with_report([notice,*keep],limit)
    if not bounded:
        raise RuntimeError(BUDGET_EXHAUSTED + ": routed evidence cannot fit the configured budget")
    return bounded, {"used":True,"omitted_tool_group_ranges":_ranges(omitted),"budget":report,
                     "delivered_refs":[message["evidence_ref"] for message in bounded if message.get("evidence_ref")]}


def install_review_policy(api, settings=None) -> None:
    """Bound replay and collect the final explanation from the pinned v1 runtime."""
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
        if not enabled and intent.get("reason") != "user_request":
            return native_review(handle, source, messages, intent)
        parent = runtime.agent(handle, source)
        provider, model = (str(task.get(key, "")).strip() for key in ("provider", "model"))
        routed = provider not in ("", "auto") and model and (provider, model) != (parent.provider, parent.model)
        budget = _review_input_token_budget(task, None if routed else parent)
        # Keep most of the aggregate budget for the system prompt, tool results
        # and follow-up calls. Serialized UTF-8 bytes bound the replay payload.
        limit = MAX_HISTORY_BYTES if budget is None else min(MAX_HISTORY_BYTES, budget // 4)
        from .evidence import organize_review_history
        organized, evidence = organize_review_history(api,handle,source,messages,intent,settings)
        bounded, coverage = bound_review_history_with_report(organized, limit)
        if settings is not None:
            from .store import Store
            store = Store(settings.state)
            try:
                store.set("review-input:" + str(intent.get("id",handle)), {**evidence,"budget":coverage})
            finally:
                store.close()
        if messages and not bounded:
            return {"run_status": "failed", "failure_reason": BUDGET_EXHAUSTED,
                    "error": "The background review input budget is too small to replay the evidence.",
                    "applied": [], "staged": [], "refused": [], "uncertain": [], "usage": None, "no_change": False,
                    "evidence_coverage":coverage}
        from agent import background_review
        digest_original = background_review._digest_history if hasattr(background_review,"_digest_history") else None
        if digest_original:
            identities = {id(message) for message in bounded}
            def routed_digest(snapshot, *args, **kwargs):
                if not any(id(message) in identities for message in snapshot):
                    return digest_original(snapshot,*args,**kwargs)
                delivered, routed_coverage = causal_digest(snapshot,digest_original,limit)
                if settings is not None:
                    store=Store(settings.state)
                    try:
                        key="review-input:"+str(intent.get("id",handle))
                        store.set(key,{**store.get(key,{}),"native_digest":routed_coverage})
                    finally:
                        store.close()
                return delivered
            background_review._digest_history = routed_digest
        try:
            outcome = native_review(handle, source, bounded, intent)
        finally:
            if digest_original:
                background_review._digest_history = digest_original
        usage = outcome.get("usage") or {}
        if (outcome.get("run_status") == "failed" and is_pending_tool_reply(outcome.get("error") or "")
                and (usage.get("provider"), usage.get("model")) == (parent.provider, parent.model)):
            # The fork may fall back to the parent after a configured route fails.
            actual_budget = _review_input_token_budget(task, parent)
            if actual_budget is not None and (usage.get("input_tokens") or 0) >= actual_budget:
                return {**outcome, "failure_reason": BUDGET_EXHAUSTED}
        return outcome

    runtime.review = review
    from .review_summary import install_summary_capture
    install_summary_capture(runtime)
