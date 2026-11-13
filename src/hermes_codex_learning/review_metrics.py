"""Aggregate review work from existing receipts without reading conversation bodies."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


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
                    "attempts_with_changes": 0, "entities": set(), "models": Counter()}
              for key in ("acceptance", "audit", "other")}
    rows = store.db.execute("""
        SELECT json_extract(f.payload,'$.source_chat') AS chat,
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
        metrics = {key: usage.get(key) for key in ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens")}
        metrics["wall_seconds"] = duration
        for key, value in metrics.items():
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                group["totals"][key] += value
                group["coverage"][key] += 1
                if key == "wall_seconds":
                    group["wall_time_sources"]["worker_run" if row["timed"] else "notice_interval"] += 1
        entities = json.loads(row["entities"]) if row["entities"] else None
        if isinstance(entities, list):
            group["coverage"]["applied_entities"] += 1
            group["entities"].update(item for item in entities if isinstance(item, str))
            group["attempts_with_changes"] += bool(entities)
    for group in groups.values():
        group["attempts"] = sum(group["outcomes"].values())
        group["entities"] = sorted(group["entities"])
        if "wall_seconds" in group["totals"]:
            group["totals"]["wall_seconds"] = round(group["totals"]["wall_seconds"], 1)
    return {"state": "available", "project": project, "date": day.isoformat(), "timezone": timezone,
            "basis": "stored_review_finished_notices", "classified_chats": {"acceptance": len(acceptance), "audit": len(audit)},
            "groups": groups}
