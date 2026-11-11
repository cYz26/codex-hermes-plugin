"""Relate admitted observations without importing histories or inventing missing bodies."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import re

from .store import ReadStore, digest, dumps


def selections(prompt: str) -> list[dict]:
    match = re.search(r"<response-annotations>\s*(\[.*?\])\s*</response-annotations>", prompt, re.S)
    if not match or len(match[1].encode()) > 65536:
        return []
    try:
        items = json.loads(match[1])
    except ValueError:
        return []
    result = []
    for item in items[:8]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        source = item.get("source") or {}
        if not isinstance(source, dict):
            source = {}
        result.append({"text":item["text"][:4096], "text_truncated":len(item["text"]) > 4096,
                       "comment":str(item.get("annotation") or "")[:4096],
                       "source":{k:v for k,v in source.items() if k in
                                 {"messageId","threadId","turnId","contentOffsets","startOffset","endOffset"}},
                       "reference_status":"unresolved"})
    return result


def _position(body):
    trace = body.get("host_trace", {})
    for kind in ("request","start"):
        position = trace.get(kind, {})
        stamp = position.get("timestamp")
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp(), "host_timestamp"
        except (AttributeError, TypeError, ValueError):
            pass
    value = body.get("evidence_provenance", {})
    stamp = value.get("request_at") or value.get("first_captured_at")
    return (stamp, "capture_arrival") if isinstance(stamp, (int,float)) else (None, "journal_arrival")


def _result_at(body):
    stamp = body.get("host_trace",{}).get("result",{}).get("timestamp")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.timestamp()
    except (AttributeError,TypeError,ValueError):
        pass
    value = body.get("evidence_provenance",{}).get("terminal_at")
    return value if isinstance(value,(int,float)) else None


def _answers(rows):
    answers, seen = [], set()
    for row in rows:
        for message in row["messages"]:
            text = message.get("content")
            if message["role"] == "assistant" and not message.get("tool_calls") and text and text not in seen:
                seen.add(text)
                answers.append((message,row))
    return answers


def _snapshot(api, handle, intent, supplied):
    service = getattr(api, "_service", None)
    db = getattr(service, "db", None)
    if db is None or "revision" not in intent:
        return [], "journal_provenance_unavailable"
    with getattr(service, "lock", nullcontext()):
        rows = db.execute("SELECT id,turn,revision,body,messages FROM observations "
                          "WHERE handle=? AND revision<=? ORDER BY revision", (handle,intent["revision"])).fetchall()
    supplied_ids = {m.get("message_uid") for m in supplied if m.get("message_uid")}
    observations = []
    for row in rows:
        original = json.loads(row[4])
        replay = [m for m in original if m.get("message_uid") in supplied_ids]
        if replay:
            observations.append({"id":row[0], "turn":row[1], "revision":row[2],
                                 "body":json.loads(row[3]), "messages":replay})
    return observations, None if observations else "explicit_or_legacy_snapshot_without_journal_ids"


def _parent_turns(store, settings, source, relation, selected=()):
    """Only recorded, admitted parent turns on this host/project and before the fork."""
    gaps, result = [], []
    if relation.get("status") != "verified":
        return result, [{"reason":relation.get("reason", "host_parent_relation_unavailable")}]
    parent, cutoff = relation["parent_chat"], relation["fork_ordinal_exclusive"]
    if parent == source["chat"] or store.get("excluded-chat:" + parent):
        return [], [{"reason":"parent_context_excluded"}]
    rows = store.db.execute("SELECT payload FROM turns WHERE json_extract(payload,'$.source.chat')=? "
                            "AND json_extract(payload,'$.source.host')=? AND json_extract(payload,'$.source.project')=?",
                            (parent,source["host"],source["project"])).fetchall()
    from .policy import admit
    from .normalize import observation
    from .host_evidence import supplement
    for row in rows:
        body = json.loads(row[0])
        turn = body["source"]["turn"]
        if store.get("withheld-turn:" + digest([parent,turn])):
            gaps.append({"turn":turn,"reason":"parent_turn_not_admitted"})
            continue
        from .workflow import USER
        if body.get("_evidence_mode") == "workflow-v1" or body.get("evidence_provenance",{}).get("body_retained") is False or body.get("user") == USER:
            gaps.append({"turn":turn,"reason":"parent_turn_body_not_retained"})
            continue
        # This supplements terminal metadata only. It never imports a parent's
        # prompt, answer or tool body from a host transcript.
        body = supplement(store, body, settings=settings, include_task=False)
        trace = body.get("host_trace", {})
        start = trace.get("request", trace.get("start", {})).get("ordinal")
        if type(start) is not int:
            gaps.append({"turn":turn,"reason":"parent_fork_position_unresolved"})
            continue
        if start >= cutoff:
            continue
        tools = {}
        for identifier, tool in body.get("tools", {}).items():
            position = tool.get("host_position", {}).get("ordinal")
            if type(position) is int and position < cutoff:
                tools[identifier] = tool
        end = trace.get("result", {}).get("ordinal")
        answer = body.get("assistant", "") if type(end) is int and end < cutoff else ""
        pruned = {**body, "tools":tools, "assistant":answer, "status":"completed"}
        assessment = admit(observation(pruned), settings.data_policy, source["project"])
        if not assessment["allowed"]:
            gaps.append({"turn":turn,"reason":"parent_turn_not_admitted"})
            continue
        if assessment.get("payload"):
            admitted = assessment["payload"]
            pruned = {**pruned, "user":admitted.get("user", ""), "assistant":admitted.get("assistant", ""),
                      "tools":{tool["call_id"]:tool for tool in admitted.get("tools", [])}}
        result.append((start,pruned))
        if body.get("assistant") and not answer:
            gaps.append({"turn":turn,"reason":"parent_result_after_fork_or_position_missing"})
    result.sort(key=lambda item:item[0])
    if len(result) > 2:
        selected_ids = {item.get("source",{}).get("messageId") for item in selected}
        chosen = {0,len(result)-1}
        chosen.update(i for i,(_,body) in enumerate(result) if selected_ids & set(body.get("host_trace",{}).get("messages",{})))
        selected_indices = sorted(chosen)[:6]
        if len(result) > len(selected_indices):
            gaps.append({"reason":"intervening_parent_turns_omitted","count":len(result)-len(selected_indices)})
        result = [result[i] for i in selected_indices]
    return [body for _,body in result], gaps or ([] if result else [{"reason":"recorded_parent_context_unavailable"}])


def _mark(message, ref, kind, source, *, scope="own", previous=None, revision=None):
    result = {**message, "evidence_ref":ref, "evidence_kind":kind, "evidence_scope":scope}
    if message.get("tool_calls"):
        result["content"] = f"[External execution evidence {ref}; source turn={source['turn']}]\n" + (message.get("content") or "")
    elif message["role"] != "tool":
        label = {"ref":ref, "kind":kind, "chat":source.get("chat"), "turn":source.get("turn"), "scope":scope}
        if revision is not None:
            label["observation_revision"] = revision
        if previous:
            label["previous_result"] = previous
            label["relationship"] = "possible_followup_to_previous_approach; review_semantics"
        result["content"] = "[Evidence " + dumps(label) + "]\n" + (message.get("content") or "")
    return result


def organize_review_history(api, handle, source, messages, intent, settings=None):
    """Build a source-bearing causal spine for the native multi-turn review copy."""
    observations, degraded = _snapshot(api,handle,intent,messages)
    report = {"version":1,"handle":handle,"intent":intent.get("id"),"target_revision":intent.get("revision"),
              "observations":len(observations),"turns":[],"coverage_gaps":[],"parent_context":[]}
    if settings is None:
        return messages, {**report, "provenance_status":degraded or "journal_available"}
    own = {}
    for row in observations:
        body = row["body"]
        if any(body.get("source", {}).get(key) != source.get(key) for key in ("host","project","chat")):
            raise ValueError("Review observation source mismatch")
        own.setdefault(row["turn"], []).append(row)
    ordered = sorted(own.values(), key=lambda rows:(_position(rows[-1]["body"])[0]
                                                    if _position(rows[-1]["body"])[0] is not None else float("inf"), rows[0]["revision"]))
    output, previous, parents = [], None, []
    conclusion_refs = []
    for number,rows in enumerate(ordered,1):
        answers = _answers(rows)
        for i,(message,row) in enumerate(answers):
            ref = f"T{number}.result" if i == len(answers)-1 else f"T{number}.result.{i+1}"
            conclusion_refs.append({"ref":ref,"revision":row["revision"],"at":_result_at(row["body"]),"turn":row["turn"]})
    store = ReadStore(settings.state)
    learning_context = {}
    try:
        learning = store.get("learning-intent:"+str(intent.get("id")),{})
        if learning and learning.get("project") == source.get("project"):
            row = store.db.execute("SELECT payload FROM learning_requests WHERE id=? AND project=? AND chat=?",
                                   (learning["request_id"],source["project"],source["chat"])).fetchone()
            if row:
                request = json.loads(row[0])
                if request["mode"] == "learn":
                    learning_context = {"mode":"learn","target":request["skill_name"],"basis":"admitted_explicit_material_request"}
        # Old workflow-only observations are an honest coverage gap, never a
        # fabricated first-round body for a newer semantic handle.
        for row in store.db.execute("SELECT payload FROM turns WHERE json_extract(payload,'$.source.chat')=? "
                "AND json_extract(payload,'$.source.host')=? AND json_extract(payload,'$.source.project')=? "
                "AND json_extract(payload,'$._evidence_mode')='workflow-v1'", (source.get("chat"),source.get("host"),source.get("project"))):
            report["coverage_gaps"].append({"turn":json.loads(row[0])["source"]["turn"],"reason":"earlier_turn_body_not_retained"})
        relation = next((rows[-1]["body"].get("host_context", {}).get("relation") for rows in ordered
                         if rows[-1]["body"].get("host_context", {}).get("relation")), {})
        # Parent metadata supplementation writes only peripheral cursors and
        # therefore uses the owner's Store connection, never the read-only view.
        if relation and relation.get("parent_chat"):
            from .store import Store
            writable = Store(settings.state)
            try:
                selected = [item for rows in ordered for item in rows[-1]["body"].get("host_context",{}).get("selections",[])]
                parents, gaps = _parent_turns(writable,settings,source,relation,selected)
            finally:
                writable.close()
            report["coverage_gaps"].extend(gaps)
            for i,body in enumerate(parents,1):
                parent_ref = f"P{i}"
                report["parent_context"].append({"ref":parent_ref,"source":body["source"],"fork":relation})
                output.append(_mark({"role":"user","content":body["user"],"observed":True},parent_ref+".request","request",body["source"],scope="parent"))
                if body.get("assistant"):
                    output.append(_mark({"role":"assistant","content":body["assistant"],"observed":True},parent_ref+".result","result",body["source"],scope="parent"))
                    previous = parent_ref+".result"
        elif relation.get("status") in {"unavailable","partial"}:
            report["coverage_gaps"].append({"reason":relation.get("reason","host_parent_relation_unavailable")})
    finally:
        store.close()
    for number, rows in enumerate(ordered,1):
        body, ref = rows[-1]["body"], f"T{number}"
        timestamp, order_basis = _position(body)
        report["turns"].append({"ref":ref,"source":body["source"],"first_revision":rows[0]["revision"],
                                "last_revision":rows[-1]["revision"],"order_basis":order_basis,"source_at":timestamp,
                                "source_events":[row["id"] for row in rows],"coverage":body.get("coverage",[]),
                                "selections":body.get("host_context",{}).get("selections",[])})
        prior = [item for item in conclusion_refs if item["turn"] != rows[0]["turn"] and
                 ((item["at"] is not None and timestamp is not None and item["at"] < timestamp) or
                  (item["at"] is None and item["revision"] < rows[0]["revision"]))]
        prior_ref = max(prior,key=lambda item:item["revision"])["ref"] if prior else previous if number == 1 else None
        request = next((message for row in rows for message in row["messages"] if message["role"] == "user"), None)
        if request:
            # Late observations carry a supplemental label. The admitted body
            # retains the actual original prompt and its provenance.
            from .workflow import USER
            retained = body.get("evidence_provenance",{}).get("body_retained") is not False and body.get("user") != USER
            if retained:
                output.append(_mark({**request,"content":body["user"]},ref+".request","request",body["source"],previous=prior_ref,revision=rows[0]["revision"]))
            else:
                report["coverage_gaps"].append({"turn":rows[0]["turn"],"reason":"request_body_not_retained"})
        answers, tool_index = _answers(rows), 0
        for row in rows:
            for message in row["messages"]:
                if message.get("tool_calls"):
                    tool_index += 1
                    output.append(_mark(message,f"{ref}.tool{tool_index}","tool_call",body["source"]))
                elif message["role"] == "tool":
                    output.append(_mark(message,f"{ref}.tool{tool_index}.result","tool_result",body["source"]))
        for i, (answer,answer_row) in enumerate(answers):
            result_ref = ref+".result" if i == len(answers)-1 else f"{ref}.result.{i+1}"
            output.append(_mark(answer,result_ref,"result",body["source"],revision=answer_row["revision"]))
            previous = result_ref
        for selection in report["turns"][-1]["selections"]:
            identifier = selection.get("source",{}).get("messageId")
            candidates = [p for p in parents] + [other[-1]["body"] for other in ordered[:number-1]]
            for candidate in candidates:
                location = candidate.get("host_trace",{}).get("messages",{}).get(identifier)
                requested_chat = selection.get("source",{}).get("threadId")
                if not location or (requested_chat and requested_chat != candidate["source"]["chat"]):
                    continue
                if candidate["source"]["chat"] != source["chat"] and location.get("ordinal",float("inf")) >= relation.get("fork_ordinal_exclusive",0):
                    continue
                text = candidate.get("assistant" if location.get("role") == "assistant" else "user","")
                if selection.get("text") and selection["text"] in text:
                    selection.update(reference_status="resolved_to_retained_turn",referenced_source=candidate["source"],host_position=location)
                    break
    if not ordered:
        # Explicit material snapshots have their own upstream admission and
        # cutoff. Label only that supplied copy; no nearest-chat lookup.
        for i,message in enumerate(messages,1):
            kind = "material" if learning_context.get("mode") == "learn" else "request" if message["role"] == "user" else "tool_result" if message["role"] == "tool" else "result"
            origin = message.get("evidence_source") or {**source,"turn":"explicit_snapshot"}
            if any(origin.get(key) != source.get(key) for key in ("host","project","chat")):
                raise ValueError("Explicit evidence source mismatch")
            output.append(_mark(message,message.get("evidence_ref",f"E{i}"),kind,origin))
    report["provenance_status"] = degraded or "journal_source_ids"
    manifest = {"role":"user","observed":True,"review_context":True,"review_manifest":True,"learning_context":learning_context,
                "content":"[Review coverage: admitted evidence only. Parent excerpts are context, not a second task to relearn. "
                          "Treat additions as improvement candidates; infer a correction only from the dialogue and changed result. "
                          "Missing or unretained bodies remain unavailable.]\n" + dumps({
                              "own_turns":len(ordered),"observations":len(observations),
                              "parent_turns":len(report["parent_context"]),"provenance_status":report["provenance_status"],
                              "coverage_gaps":report["coverage_gaps"][:8],"additional_gaps":max(0,len(report["coverage_gaps"])-8)})}
    return [manifest,*output], {**report,"learning_context":learning_context}
