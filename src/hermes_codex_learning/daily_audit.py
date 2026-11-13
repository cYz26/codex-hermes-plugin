"""Read-only audit projection. Hermes remains the sole knowledge/lifecycle owner.

Outputs are local evidence, never learning input or publication approvals. Native
attempts have no timestamp; only associated receipts can date their completion.
"""
from __future__ import annotations

import json
import copy
import hashlib
import sqlite3
import time
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .store import atomic_json, atomic_text, redact
from .review_metrics import daily as review_metrics


def read_database(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def read_json(path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def collect(home, previous, day, deadline, timezone, audit_chats, acceptance_chats):
    result = {"attempts": copy.deepcopy(previous.get("attempts", {})),
              "notices": copy.deepcopy(previous.get("notices", {})), "gaps": [], "sources": {},
              "collection": {"native_rows_read": 0, "notice_rows_read": 0},
              "cursors": dict(previous.get("cursors", {})), "artifacts": {}, "retries": [],
              "input_coverage": {}, "applications": [], "metrics": {}, "complete": True}
    receipt_updates = []
    for name, relative in (("bridge", "bridge/bridge.sqlite"), ("native", "learning/journal.sqlite")):
        path = home / relative
        db = None
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('audit collection deadline reached')
            db = read_database(path)
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            result["sources"][name] = {"path": str(path), "sampled_at": time.time()}
            table = "attempts" if name == "native" else "notices"
            maximum = db.execute(f"SELECT coalesce(max(rowid),0) FROM {table}").fetchone()[0]
            prior = previous.get("cursors", {}).get(name, {})
            position = prior.get("rowid", 0)
            if prior and (prior.get("inode") != path.stat().st_ino or maximum < position):
                result["gaps"].append({"source": name, "reason": "source_reset_baseline_rebuilt"})
                position = 0
                result["attempts" if name == "native" else "notices"] = {}
            if name == "native":
                tracked = {key for key, item in result["attempts"].items()
                           if item["state"] not in {"completed", "failed", "interrupted"}} | set(receipt_updates)
                # JSON_each avoids SQLite variable limits on a long-running library.
                for row in db.execute("""SELECT a.*,s.source FROM attempts a
                        LEFT JOIN intents i ON a.intent=i.id LEFT JOIN scopes s ON s.id=i.handle
                        WHERE a.rowid>? OR a.id IN (SELECT value FROM json_each(?))""", (position, json.dumps(sorted(tracked)))):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('audit collection deadline reached')
                    result["collection"]["native_rows_read"] += 1
                    value = json.loads(row["payload"])
                    source = json.loads(row["source"] or "{}")
                    first = result["attempts"].get(row["id"], {}).get("first_report_date", day)
                    result["attempts"][row["id"]] = {
                        "id": row["id"], "intent": row["intent"], "state": row["status"],
                        "first_report_date": first,
                        "project": source.get("project"), "chat": source.get("chat"),
                        "writes": {key: len(value.get(key) or []) for key in ("applied", "staged", "refused", "uncertain")},
                        "error": redact(value.get("error")), "usage": value.get("usage"),
                        "paid_cost": None, "finished_at": None, "time_basis": "unknown",
                    }
            else:
                for row in db.execute("SELECT * FROM notices WHERE seq>? AND json_extract(payload,'$.kind')='review.finished'", (position,)):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('audit collection deadline reached')
                    result["collection"]["notice_rows_read"] += 1
                    value = json.loads(row["payload"])
                    identity = row["id"].removesuffix(":result")
                    receipt_updates.append(identity)
                    result["notices"][identity] = {key: value.get(key) for key in (
                        "intent_id", "project", "source_chat", "run_status", "started_at", "finished_at", "usage")}
                    result["notices"][identity]["notice_at"] = row["created"]
                metadata = {row["key"]: json.loads(row["value"]) for row in db.execute(
                    "SELECT key,value FROM meta WHERE key IN ('artifact-read-cache','publications-v1','publication-decisions','loop-progress')")}
                for skill, item in metadata.get("artifact-read-cache", {}).items():
                    artifact = item.get("artifact", {})
                    published = metadata.get("publications-v1", {}).get(skill, {}).get("artifact", {})
                    result["artifacts"][skill] = {key: artifact.get(key) for key in ("version", "files", "resource_dir")}
                    result["artifacts"][skill].update(published_version=published.get("version"),
                        publication=metadata.get("publication-decisions", {}).get(skill))
                result["worker_snapshot"] = metadata.get("loop-progress")
                result["retries"] = [{**json.loads(row['payload']), 'state': row['state']}
                                     for row in db.execute("SELECT payload,state FROM review_retries ORDER BY id")]
                result["applications"] = [json.loads(row[0]) for row in db.execute("SELECT payload FROM application_evidence ORDER BY created,id")]
                for row in db.execute("""SELECT key, json_extract(value,'$.budget') budget,
                        json_extract(value,'$.coverage_gaps') gaps, json_extract(value,'$.native_digest') native
                        FROM meta WHERE key LIKE 'review-input:%'"""):
                    result["input_coverage"][row["key"].removeprefix("review-input:")] = {
                        "budget": json.loads(row["budget"] or "null"), "review_gaps": json.loads(row["gaps"] or "[]"),
                        "native_digest": json.loads(row["native"] or "null")}
                projects = [row[0] for row in db.execute("SELECT DISTINCT json_extract(payload,'$.project') FROM notices") if row[0]]
                for project in projects:
                    result['metrics'][project] = review_metrics(SimpleNamespace(db=db), project, day, timezone,
                        audit_chats=audit_chats, acceptance_chats=acceptance_chats)
            result["cursors"][name] = {"rowid": maximum, "inode": path.stat().st_ino}
        except (OSError, sqlite3.Error, ValueError) as exc:
            result['complete'] = False
            result["gaps"].append({"source": name, "reason": type(exc).__name__ + ": " + str(exc)})
        finally:
            if db is not None:
                db.close()
    return result


def issues(attempts, retries):
    result = []
    for item in attempts.values():
        if item['state'] == 'completed' and not item['writes']['uncertain']:
            continue
        retry = next((r for r in retries if item['intent'] in
                      {r.get('root_intent'), r.get('parent_intent'), r.get('child_intent')}), {})
        state = retry.get('state') or ('running' if item['state'] == 'running' else 'needs_review')
        result.append({'id': 'review:' + item['intent'], 'attempt': item['id'], 'state': state,
                       'root_intent': retry.get('root_intent', item['intent']),
                       'first_report_date': item['first_report_date'],
                       'native_state': item['state'], 'disposition': retry.get('disposition'),
                       'successor_intent': retry.get('child_intent'), 'reason': retry.get('reason', item['error'])})
    return result


def render(report):
    lines = [f"# 自进化审查证据 · {report['date']}", "",
             f"时区：{report['timezone']}；采样：{report['generated_at']}；采集状态：{report['state']}。",
             "", "本文件为只读证据汇总，语义审查与实际收益仍需核对。", "",
             f"受审日已记录终态：{len(report['window_attempts'])}；无可确认日期：{len(report['undated_attempts'])}。",
             "", "| 尝试 | 项目 | 执行状态 | 已写入／待审／未确认 | 时间依据 |", "|---|---|---|---|---|"]
    for identity in report["window_attempts"]:
        item = report["attempts"][identity]
        writes = item["writes"]
        lines.append(f"| `{identity}` | {item['project']} | {item['state']} | "
                     f"{writes['applied']} / {writes['staged']} / {writes['uncertain']} | {item['time_basis']} |")
    lines += ["", "完整逐次台账、覆盖缺口、版本和来源见同名 JSON。费用未知；完成不代表完整学习或有效应用。", ""]
    lines += [f"跨日问题：{len(report['issues'])}；观察到的技能版本变化：{len(report['artifact_changes'])}。", "",
              "运行状态来自有采样时间的本地快照，不代表实时服务探测。缺少宿主全量分母，覆盖率未知。", ""]
    for item in report['issues']:
        lines.append(f"- `{item['id']}`：{item['state']}；原始状态 {item['native_state']}。")
    if report["gaps"]:
        lines += ["覆盖缺口：", ""] + ["- " + str(item) for item in report["gaps"]]
    return "\n".join(lines) + "\n"


def export(home, output, day, timezone="Asia/Shanghai", *, now=None, timeout=600,
           audit_chats=(), acceptance_chats=()):
    home, output = Path(home).resolve(), Path(output).resolve()
    if output == home or any(output.is_relative_to(home / name) for name in
                             ('skills', 'memories', 'learning', 'bridge', 'pending')):
        raise ValueError('Audit output must be separate from knowledge, runtime and learning databases')
    if set(audit_chats) & set(acceptance_chats):
        raise ValueError('A chat cannot be both audit and acceptance')
    current = time.time() if now is None else now
    zone = ZoneInfo(timezone)
    selected = date.fromisoformat(day)
    day = selected.isoformat()
    start = datetime.combine(selected, day_time.min, zone).timestamp()
    end = datetime.combine(selected + timedelta(days=1), day_time.min, zone).timestamp()
    previous = read_json(output / "collection.json", {})
    result = collect(home, previous, day, time.monotonic() + timeout, timezone, audit_chats, acceptance_chats)
    for identity, notice in result["notices"].items():
        attempt = result["attempts"].get(identity)
        if attempt is None:
            result["gaps"].append({"source": "bridge", "reason": "receipt_without_retained_attempt", "id": identity})
            continue
        attempt["finished_at"] = notice["finished_at"] if notice["finished_at"] is not None else notice["notice_at"]
        attempt["started_at"] = notice["started_at"]
        attempt["time_basis"] = "worker_run" if notice["finished_at"] is not None else "notice_time_only"
    for attempt in result['attempts'].values():
        chat = attempt.get('chat')
        attempt['task_class'] = 'audit' if chat in audit_chats else 'acceptance' if chat in acceptance_chats else 'unclassified'
        attempt['input_coverage'] = result['input_coverage'].get(attempt['intent'])
    window = [key for key, item in result["attempts"].items()
              if item["finished_at"] is not None and start <= item["finished_at"] < end]
    undated = [key for key, item in result["attempts"].items() if item["finished_at"] is None]
    if undated:
        result["gaps"].append({"source": "native", "reason": "attempt_time_unknown", "count": len(undated)})
    result.update(schema_version=1, date=selected.isoformat(), timezone=timezone, generated_at=current,
                  window={"start": start, "end": end}, window_attempts=window, undated_attempts=undated,
                  state="partial" if result["gaps"] else "collected", semantic_review="pending",
                  effectiveness="not_inferred")
    existing = read_json(output / (day + ".json"), {})
    changes = {item["name"]: item for item in existing.get("artifact_changes", [])}
    for name, item in result["artifacts"].items():
        before = previous.get("artifacts", {}).get(name)
        if before and before.get("version") != item.get("version"):
            old = changes.get(name, {}).get("previous_version", before.get("version"))
            changes[name] = {"name": name, "previous_version": old, "version": item.get("version"),
                             "observed_at": current, "basis": "snapshot_comparison_not_historical_write_time",
                             "changed_files": sorted(key for key in set(before.get("files") or {}) | set(item.get("files") or {})
                                 if (before.get("files") or {}).get(key) != (item.get("files") or {}).get(key))}
    result["artifact_changes"] = list(changes.values())
    result['issues'] = issues(result['attempts'], result['retries'])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(output / (day + ".json"), result)
    atomic_text(output / (day + ".md"), render(result))
    if result['complete']:
        checkpoint = {key: result[key] for key in ("cursors", "attempts", "notices", "artifacts")}
        checkpoint.update(last_collected_at=current,
                          last_completed_review_date=previous.get("last_completed_review_date"),
                          reviews=previous.get('reviews', {}))
        atomic_json(output / "collection.json", checkpoint)
    latest = read_json(output / 'latest.json', {})
    if not latest or day >= latest.get('date', ''):
        atomic_json(output / 'latest.json', {'date': day, 'generated_at': current, 'state': result['state'],
                                            'report': day + '.md', 'semantic_review': 'pending'})
    return result


def mark_reviewed(output, day, note, snapshot_sha256):
    """Record an explicit analyst conclusion against the exact inspected export."""
    output, note = Path(output), Path(note)
    day = date.fromisoformat(day).isoformat()
    path = output / (day + '.json')
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != snapshot_sha256 or not note.read_text().strip():
        raise ValueError('Review requires the inspected snapshot hash and a nonempty conclusion file')
    snapshots = output / 'snapshots'
    snapshots.mkdir(exist_ok=True)
    atomic_text(snapshots / (actual + '.json'), raw.decode())
    state = read_json(output / 'collection.json', {})
    state['last_completed_review_date'] = max(day, state.get('last_completed_review_date') or day)
    state.setdefault('reviews', {})[day] = {'snapshot_sha256': actual, 'note': str(note.resolve()),
        'note_sha256': hashlib.sha256(note.read_bytes()).hexdigest(), 'completed_at': time.time()}
    atomic_json(output / 'collection.json', state)
    latest = read_json(output / 'latest.json', {})
    if latest.get('date') == day:
        atomic_json(output / 'latest.json', {**latest, 'semantic_review': 'reviewed',
                                           'snapshot_sha256': actual, 'note': str(note.resolve())})
    return state['reviews'][day]
