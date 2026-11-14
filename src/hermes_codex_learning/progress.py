"""Read current per-turn positions; a service response is not end-to-end health."""
from __future__ import annotations

import json
import time

from .delegation import _result_object
from .feedback import applications_for_turn, deliveries_for_turn, test_references
from .normalize import BACKGROUND_EXIT_PENDING
from .store import digest


def _mcp_recommendations(store, project, chat, turn):
    rows = store.db.execute("""
        SELECT json_extract(payload,'$.tool_use_id'), payload
        FROM events WHERE json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.turn_id')=?
          AND json_extract(payload,'$.bridge_project')=? AND json_extract(payload,'$.hook_event_name')='PostToolUse'
          AND json_extract(payload,'$.tool_name')='mcp__hermes_learning__context'
        ORDER BY rowid DESC LIMIT 5
    """, (chat, turn, project))
    recommendations = []
    for tool_id, raw in rows:
        value = _result_object(json.loads(raw).get("tool_response"))
        if not tool_id or not value or value.get("project") != project:
            continue
        names = value.get("recommended_skills", [])
        skills = value.get("skills", [])
        if not isinstance(names, list) or not isinstance(skills, list):
            continue
        versions = {item.get("name"): item.get("artifact_version") for item in skills
                    if isinstance(item, dict) and isinstance(item.get("name"), str)}
        recommendations.append({
            "source_event": {"chat": chat, "turn": turn, "tool_call": tool_id},
            "skills": [{"name": name, "artifact_version": versions.get(name)} for name in names[:5]
                       if isinstance(name, str)],
        })
    return recommendations


def summarize(store, *, project=None, chat=None, turn=None, limit=20, after_cursor=0, now=None):
    now = time.time() if now is None else now
    if type(limit) is not int or not 1 <= limit <= 100 or type(after_cursor) is not int or after_cursor < 0:
        raise ValueError("Progress requires limit 1–100 and a nonnegative cursor")
    if any(value is not None and (not isinstance(value,str) or not value or len(value)>256) for value in (chat,turn)):
        raise ValueError("Progress chat/turn identifiers must be nonempty strings")
    where, args = ["json_extract(e.payload,'$.session_id') IS NOT NULL", "json_extract(e.payload,'$.turn_id') IS NOT NULL"], []
    for field,value in (("bridge_project",project),("session_id",chat),("turn_id",turn)):
        if value is not None:
            where.append(f"json_extract(e.payload,'$.{field}')=?")
            args.append(value)
    query = """WITH captured AS (
        SELECT json_extract(e.payload,'$.session_id') chat,json_extract(e.payload,'$.turn_id') turn,
          json_extract(e.payload,'$.bridge_project') project,json_extract(e.payload,'$.bridge_host') host,
          MIN(e.rowid) cursor,MAX(e.rowid) last_captured_position,MAX(CASE WHEN delivered IS NOT NULL THEN e.rowid END) last_consumed_position,
          COUNT(*) captured_events,SUM(delivered IS NULL) pending_events,MIN(created) captured_at,
          SUM(CASE WHEN json_extract(e.payload,'$.bridge_admission.body_retained')=0 OR json_extract(e.payload,'$.bridge_evidence_mode')='workflow-v1' THEN 1 ELSE 0 END) unretained_events,
          MAX(delivered) consumed_at,MIN(CASE WHEN delivered IS NULL THEN created END) pending_at
        FROM events e WHERE """ + " AND ".join(where) + """ GROUP BY chat,turn,project,host)
        SELECT c.* FROM captured c
        WHERE c.cursor>? ORDER BY c.cursor LIMIT ?"""
    rows = store.db.execute(query,[*args,after_cursor,limit+1]).fetchall()
    publications, decisions = store.get("publications-v1",{}), store.get("publication-decisions",{})
    queue = store.get("review-queue",{})
    items = []
    for captured in rows[:limit]:
        # Bound the page before touching retained turn bodies. Joining every
        # turn through JSON source fields scales with the whole history.
        row = {**dict(captured), "turn_key":None, "observed_revision":None,
               "turn_status":None, "ended_by":None, "evidence_mode":None, "gaps":None}
        key = digest([row["chat"],row["turn"]])
        metadata = store.db.execute("""SELECT id turn_key,observed_revision,
          json_extract(payload,'$.status') turn_status,json_extract(payload,'$._ended_by') ended_by,
          json_extract(payload,'$._evidence_mode') evidence_mode,json_extract(payload,'$.coverage') gaps
          FROM turns WHERE id=? AND json_extract(payload,'$.source.host')=?
          AND json_extract(payload,'$.source.project')=?""", (key,row["host"],row["project"])).fetchone()
        if metadata:
            row.update(dict(metadata))
        admission = store.get("turn-admission:"+key,{})
        receipt = store.get("observation-receipt:"+key,{})
        native_revision, handle = receipt.get("revision"), receipt.get("handle")
        reviewed = store.get("review-progress:"+str(handle),{}) if handle else {}
        reviewed_revision = reviewed.get("revision")
        covered = (type(native_revision) is int and type(reviewed_revision) is int and native_revision <= reviewed_revision)
        review_state = reviewed.get("state","not_requested") if covered else "awaiting_review" if receipt else "not_observed"
        pending = [intent for intent in queue.get("intents",[]) if intent.get("handle") == handle
                   and type(native_revision) is int and intent.get("revision",-1) >= native_revision]
        if pending and not covered:
            review_state = pending[-1].get("status","queued")
        review_reason = reviewed.get("reason") if covered else pending[-1].get("reason") if pending else None
        usage = reviewed.get("usage") or {}
        own_ref = next((item["ref"] for item in reviewed.get("input",{}).get("turns",[])
                        if item.get("source",{}).get("turn") == row["turn"] and item.get("source",{}).get("chat") == row["chat"]), None)
        candidates = []
        if covered and own_ref:
            for claim in reviewed.get("candidates",[]):
                refs = [basis.get("ref","") for basis in claim.get("basis",[])]
                if not any(ref.startswith(own_ref+".") for ref in refs):
                    continue
                candidates.append({key:claim.get(key) for key in ("state","tool","name","classification","reason") if claim.get(key) is not None})
        publication = []
        for name in sorted({item["name"] for item in candidates if item.get("name")}):
            expected = reviewed.get("versions",{}).get(name)
            published = publications.get(name,{}).get("artifact",{}).get("version")
            check = decisions.get(name,{})
            publication.append({"name":name,"candidate_version":expected,"published_version":published,
                                "state":"published" if expected and published == expected else "withheld" if check.get("allowed") is False else "not_published",
                                "reason":check.get("reason")})
        gaps = json.loads(row["gaps"] or "[]")
        if row["evidence_mode"] == "workflow-v1":
            gaps.append("turn_body_not_retained")
        if row["observed_revision"] and not receipt:
            gaps.append("legacy_native_position_not_recorded")
        review_input = reviewed.get("input",{}) if covered else {}
        recall = store.get("recall-offer:"+digest([row["chat"],row["turn"]]),{})
        source = row["project"], row["chat"], row["turn"]
        loaded = deliveries_for_turn(store, *source)
        applications = [{key: value.get(key) for key in (
            "id", "name", "version", "application", "validation", "effectiveness",
            "delivery_source_event", "events", "validation_reason", "superseded_by")}
            for value in applications_for_turn(store, *source)]
        execution = {"turn_status": row["turn_status"], "terminal_hook": row["ended_by"], "correctness": "not_inferred"}
        if BACKGROUND_EXIT_PENDING in gaps:
            execution["pending_processes"] = [dict(process) for process in store.db.execute("""
                SELECT tool.key tool_call, json_extract(tool.value,'$.process') session_id, 'running' status,
                       json_extract(tool.value,'$.background_session.observed_at') observed_at
                FROM turns t, json_each(t.payload,'$.tools') tool WHERE t.id=?
                  AND json_extract(tool.value,'$.status')='running'
                  AND json_extract(tool.value,'$.background_session') IS NOT NULL LIMIT 20
            """, (key,))]
        if project and chat:
            execution["test_references"] = test_references(store, project, chat, row["turn"])
        mcp_recommendations = _mcp_recommendations(store, project, chat, row["turn"]) if project and chat else []
        items.append({"source":{"host":row["host"],"project":row["project"],"chat":row["chat"],"turn":row["turn"]},
            "cursor":row["cursor"],"capture":{"events":row["captured_events"],"first_at":row["captured_at"],
                                               "body_retained":row["unretained_events"] < row["captured_events"],
                                               "body_coverage":"partial" if 0 < row["unretained_events"] < row["captured_events"] else "omitted" if row["unretained_events"] else "retained"},
            "consumption":{"state":"pending" if row["pending_events"] else "consumed","pending_events":row["pending_events"],
                           "backlog_seconds":max(0,now-row["pending_at"]) if row["pending_at"] else 0,"last_at":row["consumed_at"]},
            "admission":admission or {"state":"observed" if row["observed_revision"] else "not_consumed" if row["pending_events"] else "awaiting_terminal_evidence"},
            "dialogue_signal":[record[0] for record in store.db.execute("SELECT kind FROM learning_signals WHERE chat=? AND turn=? AND kind IN ('user_followup','user_correction')",(row["chat"],row["turn"]))],
            "execution":execution,
            "review":{"state":review_state,"intent":reviewed.get("intent") if covered else None,
                      "reason":review_reason,"usage":{key:usage[key] for key in
                          ("provider","model","input_tokens","output_tokens","cache_read_tokens","api_calls") if key in usage} if covered else None,
                      "covers_native_revision":covered,"started_at":reviewed.get("started_at") if covered else None,
                      "finished_at":reviewed.get("finished_at") if covered else None,
                      "writes":{"applied":reviewed.get("applied_count",0),"staged":reviewed.get("staged_count",0),"scope":"review_intent"} if covered else None},
            "candidates":candidates,"publication":publication,"recall":{"recommended_skills":recall.get("names",[]),
                "hook_context_returned":recall.get("context_returned",False),
                "offered_skills":recall.get("offered_skills") if recall.get("context_returned") else [],
                "offered_versions":recall.get("offered_versions") if recall.get("context_returned") else {},
                "mcp_recommendations":mcp_recommendations,
                "body_deliveries":loaded,"application_evidence":applications},
            "coverage":{"gaps":sorted(set(gaps)),"review_gaps":review_input.get("coverage_gaps",[]),"budget":review_input.get("budget"),
                        "native_digest":review_input.get("native_digest")},
            "last_position":{"captured_event":row["last_captured_position"],"consumed_event":row["last_consumed_position"],
                             "native_revision":native_revision,"review_revision":reviewed_revision if covered else None},
            "input_retry":store.get("input-retry:"+key)})
    pending = store.db.execute("SELECT COUNT(*),MIN(created) FROM events WHERE delivered IS NULL").fetchone()
    phase = store.get("loop-progress",{})
    refusals = [json.loads(row[0]) for row in store.db.execute("SELECT value FROM meta WHERE key LIKE 'capture-refusal:%' "
        "AND (? IS NULL OR json_extract(value,'$.project')=?) AND (? IS NULL OR json_extract(value,'$.chat')=?) "
        "AND (? IS NULL OR json_extract(value,'$.turn')=?) ORDER BY json_extract(value,'$.at') DESC LIMIT ?",
        (project,project,chat,chat,turn,turn,limit))]
    return {"generated_at":now,"turns":items,"has_more":len(rows)>limit,
            "capture_refusals":refusals,
            "next_cursor":rows[min(len(rows),limit)-1]["cursor"] if rows else after_cursor,
            "backlog":{"pending_events":pending[0],"oldest_capture_at":pending[1],
                       "oldest_age_seconds":max(0,now-pending[1]) if pending[1] else 0},
            "worker_position":{**phase,"age_seconds":max(0,now-phase["updated_at"]) if phase.get("updated_at") else None},
            "review_queue":{"updated_at":queue.get("updated_at"),"items":len(queue.get("intents",[])),
                            "age_seconds":max(0,now-queue["updated_at"]) if queue.get("updated_at") else None},
            "semantics":"Capture, admission, review, candidate generation, publication, recall and host validation are separate facts."}
