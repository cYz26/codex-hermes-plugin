"""Aggregate review work from existing receipts without reading conversation bodies."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _measure(bucket, metrics):
    for key, value in metrics.items():
        bucket["totals"][key] += value
        bucket["coverage"][key] += 1


def _skill_evidence(store, project, start, end):
    # A call that reads multiple files from one version counts once. Match
    # applications to that call and version, including feedback submitted later.
    rows = store.db.execute("""
        WITH deliveries AS (
            SELECT json_extract(value,'$.name') name, json_extract(value,'$.version') version,
                   json_extract(value,'$.source_event') source_event,
                   MIN(json_extract(value,'$.confirmed_at')) confirmed_at
            FROM meta WHERE key LIKE 'issued-delivery:%' AND json_extract(value,'$.project')=?
                AND json_array_length(value,'$.source_event')=3
            GROUP BY 1,2,3
        ), applications AS (
            SELECT json_extract(payload,'$.name') name, json_extract(payload,'$.version') version,
                   json_extract(payload,'$.delivery_source_event') source_event,
                   MAX(CASE WHEN json_array_length(payload,'$.test_outcomes')>0
                            AND json_extract(payload,'$.validation')='host_test_passed' THEN 1 ELSE 0 END) passed,
                   MAX(CASE WHEN json_array_length(payload,'$.test_outcomes')>0
                            AND json_extract(payload,'$.validation')='host_test_failed' THEN 1 ELSE 0 END) failed
            FROM application_evidence WHERE json_extract(payload,'$.project')=?
                AND json_extract(payload,'$.application')='self_reported'
                AND json_extract(payload,'$.validation')<>'superseded'
            GROUP BY 1,2,3
        )
        SELECT d.name,d.version,COUNT(d.confirmed_at) confirmed_deliveries,
               COUNT(CASE WHEN d.confirmed_at IS NOT NULL THEN a.source_event END) reported_applications,
               SUM(CASE WHEN d.confirmed_at IS NOT NULL THEN COALESCE(a.passed,0) ELSE 0 END) host_test_passed,
               SUM(CASE WHEN d.confirmed_at IS NOT NULL THEN COALESCE(a.failed,0) ELSE 0 END) host_test_failed,
               SUM(d.confirmed_at IS NULL) undated
        FROM deliveries d LEFT JOIN applications a USING(name,version,source_event)
        WHERE d.confirmed_at IS NULL OR (d.confirmed_at>=? AND d.confirmed_at<?)
        GROUP BY d.name,d.version ORDER BY d.name,d.version
    """, (project, project, start, end)).fetchall()
    return {"basis": "confirmed_delivery_call_and_version", "application_state_at_read": True,
            "undated_confirmed_deliveries": sum(row["undated"] for row in rows),
            "versions": [{key: row[key] for key in row.keys() if key != "undated"}
                         for row in rows if row["confirmed_deliveries"]]}


def daily(store, project, day, timezone, *, acceptance_chats=(), audit_chats=()):
    acceptance = set(acceptance_chats)
    audit = set(audit_chats)
    if acceptance & audit:
        raise ValueError("A chat cannot be both acceptance and audit")
    day = date.fromisoformat(day)
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).timestamp()
    end = datetime.combine(day + timedelta(days=1), time.min, zone).timestamp()
    groups = {key: {"outcomes": Counter(), "coverage": Counter(), "totals": Counter(), "wall_time_sources": Counter(),
                    "attempts_with_changes": 0, "entities": set(), "models": Counter(),
                    "by_reason": {}, "by_executor": {}}
              for key in ("acceptance", "audit", "other")}
    rows = store.db.execute("""
        SELECT json_extract(f.payload,'$.source_chat') AS chat,
               json_extract(f.payload,'$.reason') AS reason,
               json_extract(f.payload,'$.executor') AS executor,
               json_extract(f.payload,'$.run_status') AS outcome,
               json_extract(f.payload,'$.usage') AS usage,
               json_extract(f.payload,'$.applied_entities') AS entities,
               json_extract(f.payload,'$.finished_at') IS NOT NULL
                   AND json_extract(f.payload,'$.started_at') IS NOT NULL AS timed,
               coalesce(json_extract(f.payload,'$.started_at'), s.created) AS started,
               coalesce(json_extract(f.payload,'$.finished_at'), f.created) AS finished
        FROM notices f
        LEFT JOIN notices s ON f.id LIKE '%:result' AND s.id=substr(f.id,1,length(f.id)-7)||':start'
        WHERE f.created>=? AND f.created<? AND json_extract(f.payload,'$.kind')='review.finished'
            AND json_extract(f.payload,'$.project')=?
        ORDER BY f.created
    """, (start, end, project))
    for row in rows:
        chat = row["chat"]
        group = groups["acceptance" if chat in acceptance else "audit" if chat in audit else "other"]
        group["outcomes"][row["outcome"] or "unknown"] += 1
        usage = json.loads(row["usage"] or "{}")
        model = usage.get("model")
        if model:
            group["models"][model] += 1
        started, finished = row["started"], row["finished"]
        duration = finished - started if type(started) in (int, float) and type(finished) in (int, float) else None
        candidates = {key: usage.get(key) for key in ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens")}
        candidates["wall_seconds"] = duration
        metrics = {key: value for key, value in candidates.items()
                   if type(value) in (int, float) and math.isfinite(value) and value >= 0}
        _measure(group, metrics)
        if "wall_seconds" in metrics:
            group["wall_time_sources"]["worker_run" if row["timed"] else "notice_interval"] += 1
        for dimension in ("reason", "executor"):
            bucket = group["by_" + dimension].setdefault(row[dimension] or "unknown",
                {"attempts": 0, "coverage": Counter(), "totals": Counter()})
            bucket["attempts"] += 1
            _measure(bucket, metrics)
        entities = json.loads(row["entities"]) if row["entities"] else None
        if isinstance(entities, list):
            group["coverage"]["applied_entities"] += 1
            group["entities"].update(item for item in entities if isinstance(item, str))
            group["attempts_with_changes"] += bool(entities)
    for group in groups.values():
        group["attempts"] = sum(group["outcomes"].values())
        group["entities"] = sorted(group["entities"])
        for bucket in (group, *group["by_reason"].values(), *group["by_executor"].values()):
            if "wall_seconds" in bucket["totals"]:
                bucket["totals"]["wall_seconds"] = round(bucket["totals"]["wall_seconds"], 1)
    return {"state": "available", "project": project, "date": day.isoformat(), "timezone": timezone,
            "basis": "stored_review_finished_notices", "classified_chats": {"acceptance": len(acceptance), "audit": len(audit)},
            "groups": groups, "paid_cost": None, "skill_evidence": _skill_evidence(store, project, start, end)}
