"""Durable cooldowns around native, receipt-preserving successor reviews."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

from .review_deadline import REVIEW_DEADLINE
from .review_input import BUDGET_EXHAUSTED, is_pending_tool_reply
from .enterprise import scrub
from .store import dumps


def retry_reason(outcome):
    if outcome.get("run_status") == "unknown" or outcome.get("uncertain"):
        return "unconfirmed_outcome"
    if outcome.get("failure_reason") == REVIEW_DEADLINE:
        return REVIEW_DEADLINE
    error = str(outcome.get("error") or "")
    if outcome.get("failure_reason") == BUDGET_EXHAUSTED:
        return "non_transient"
    if re.search(r"(?i)\b(?:401|403)\b|permission denied|access denied|authentication|invalid.api.key", error):
        return "non_transient"
    if outcome.get("run_status") == "interrupted":
        return "interrupted"
    if re.search(r"(?i)\b429\b|rate.?limit", error):
        return "rate_limit"
    if re.search(r"(?i)\b(?:500|502|503|504)\b|internal server error|service unavailable|bad gateway", error):
        return "provider_unavailable"
    if re.search(r"(?i)\bCodex stream produced no parsed stream event within \d+s \(TTFB threshold: \d+s\)", error):
        return "connection"
    if re.search(r"(?i)timeout|timed out|connection(?:error|reseterror|refusederror|abortederror| reset| refused| aborted)|"
                 r"api.?connection|connecterror|remoteprotocolerror|brokenpipeerror|network(?:error| unreachable)|"
                 r"remote.*disconnect|provider disconnected", error):
        return "connection"
    if is_pending_tool_reply(error):
        return "interrupted"
    return "non_transient"


def retry_text(value):
    if not value:
        return ""
    state = value.get("state")
    if state == "closed_without_retry":
        return "已人工核对并关闭，原执行结果保留，不再重试"
    if state == "waiting":
        when = datetime.fromtimestamp(value["next_retry_at"]).astimezone().strftime("%H:%M:%S %Z")
        reason = {"rate_limit": "模型限流", "connection": "连接暂时中断",
                  "provider_unavailable": "模型服务暂时不可用", "interrupted": "复盘中断"}.get(value.get("reason"), "暂时性故障")
        return f"{reason}，预计 {when} 自动重试（第 {value['next_retry_index']}/{value['max_retries']} 次）"
    if state == "completed" and value.get("retry_index"):
        return f"第 {value['retry_index']} 次重试完成"
    if state == "exhausted":
        return f"已达到 {value['max_retries']} 次重试上限，执行记录已保留"
    if state == "blocked":
        return {"unconfirmed_outcome": "执行结果待核对，已暂停自动重试",
                REVIEW_DEADLINE: "本轮复盘达到总时限，已停止自动重试"}.get(value.get("reason"), "此错误已暂停自动重试")
    if state == "disabled":
        return "自动重试已关闭"
    if state == "superseded":
        return "后续复盘已覆盖这份执行记录，已取消重试"
    return ""


class ReviewRetryQueue:
    def __init__(self, settings, store, *, clock=None):
        self.settings, self.store = settings, store
        self.clock = clock or time.time

    def save(self, root, value):
        with self.store.db:
            saved = self.store.db.execute(
                "INSERT INTO review_retries VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "handle=excluded.handle,state=excluded.state,due=excluded.due,payload=excluded.payload,updated=excluded.updated "
                "WHERE review_retries.state!='closed_without_retry'",
                (root, value["handle"], value["state"], value.get("next_retry_at"), dumps(value), self.clock()))
            if not saved.rowcount:
                return json.loads(self.store.db.execute("SELECT payload FROM review_retries WHERE id=?", (root,)).fetchone()[0])
        return value

    def record(self, intent, attempt, source=None):
        if intent.get("reason") == "user_request" or intent.get("request_id") or intent.get("attended"):
            return None
        deadline = self.store.get("review-deadline:" + str(attempt.get("id")))
        if deadline:
            attempt = {**attempt, "failure_reason": REVIEW_DEADLINE, "error": attempt.get("error") or deadline["error"]}
        root = intent.get("retry_root", intent["id"])
        row = self.store.db.execute("SELECT payload FROM review_retries WHERE id=?", (root,)).fetchone()
        old = json.loads(row[0]) if row else {}
        index, state = intent.get("retry_index", 0), attempt.get("run_status")
        if old.get("retry_index", -1) > index:
            return old
        if old.get("state") in {"superseded", "closed_without_retry"}:
            return old
        if state in {"completed", "superseded"}:
            if not old and not intent.get("retry_root"):
                return None
            value = {**old, "handle": intent["handle"], "state": state, "retry_index": index,
                     "next_retry_at": None, "last_attempt": attempt.get("id")}
            return self.save(root, value)
        if state not in {"failed", "interrupted", "unknown"}:
            return None
        reason = retry_reason(attempt)
        value = {"root_intent": root, "handle": intent["handle"], "parent_intent": intent["id"],
                 "last_attempt": attempt.get("id"), "retry_index": index,
                 "next_retry_index": index + 1, "max_retries": self.settings.review_retry_limit,
                 "project": (source or {}).get("project"), "chat": (source or {}).get("chat"),
                 "reason": reason, "error": scrub(str(attempt.get("error") or ""))[:1000],
                 "outcome_status": state, "next_retry_at": None}
        if attempt.get("failure_reason") == REVIEW_DEADLINE:
            value["failure_reason"] = REVIEW_DEADLINE
        if not self.settings.review_retry_enabled:
            value["state"] = "disabled"
        elif reason in {"non_transient", "unconfirmed_outcome", REVIEW_DEADLINE}:
            value["state"] = "blocked"
        elif index >= self.settings.review_retry_limit:
            value["state"] = "exhausted"
        else:
            value["state"] = "waiting"
            delay = min(self.settings.review_retry_delay * 2**index, self.settings.review_retry_max_delay)
            value["next_retry_at"] = self.clock() + delay
            if (old.get("last_attempt") == value["last_attempt"] and old.get("reason") == reason
                    and old.get("outcome_status") == state and old.get("state") in {"waiting", "queued"}):
                return old
        return self.save(root, value) if value != old else old

    def sync(self, api, *, force=False, source_for=None):
        if not callable(getattr(api, "retry_review", None)):
            return []
        now = self.clock()
        if not force and now - self.store.get("review-retries:last-scan", 0) < 30:
            return []
        cursor = self.store.get("review-retries:cursor", 0)
        page = api.read(kind="review_recovery", after_cursor=cursor)
        changes = []
        for item in page["items"]:
            root = item["intent"].get("retry_root", item["intent"]["id"])
            row = self.store.db.execute("SELECT payload FROM review_retries WHERE id=?", (root,)).fetchone()
            old = json.loads(row[0]) if row else None
            source = source_for(item["intent"]["handle"], item["source"]) if source_for else item["source"]
            value = self.record(item["intent"], item["attempt"], source)
            if value and value != old:
                changes.append(value)
        if page["has_more"]:
            self.store.set("review-retries:cursor", page["next_cursor"])
        else:
            # Repeat a paginated scan so a completion after a crash is reconciled,
            # including changes to an existing journal position.
            self.store.set("review-retries:cursor", 0)
            self.store.set("review-retries:last-scan", now)
        return changes

    def request(self, attempt_id, *, dry_run=True):
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 200 or not isinstance(dry_run, bool):
            raise ValueError("Retry requires an attempt ID and a boolean preview")
        with self.store.db:
            if not dry_run:
                self.store.db.execute("BEGIN IMMEDIATE")
            row = self.store.db.execute("SELECT id,payload FROM review_retries "
                                         "WHERE json_extract(payload,'$.last_attempt')=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("Attempt is not in the recovery queue; inspect learning_status after discovery")
            value = json.loads(row["payload"])
            eligible = self.settings.review_retry_enabled and value["state"] == "waiting"
            if eligible and not dry_run:
                value["next_retry_at"] = self.clock()
                self.store.db.execute("UPDATE review_retries SET due=?,payload=json_set(payload,'$.next_retry_at',?),"
                                      "updated=? WHERE id=? AND state='waiting'",
                                      (value["next_retry_at"], value["next_retry_at"], self.clock(), row["id"]))
        return {"attempt_id": attempt_id, "state": value["state"], "eligible": eligible,
                "applied": eligible and not dry_run, "next_retry_at": value.get("next_retry_at"),
                "reason": value.get("reason"), "max_retries": self.settings.review_retry_limit}

    def close_without_retry(self, attempt_id, *, note, evidence, dry_run=True):
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 200 or not isinstance(dry_run, bool):
            raise ValueError("Review closure requires an attempt ID and a boolean preview")
        if not all(isinstance(value, str) and 0 < len(value.strip()) <= 1000 for value in (note, evidence)):
            raise ValueError("Review closure requires a note and an evidence reference, each up to 1000 characters")
        note, evidence = scrub(note.strip()), scrub(evidence.strip())
        with self.store.db:
            if not dry_run:
                self.store.db.execute("BEGIN IMMEDIATE")
            row = self.store.db.execute("SELECT id,payload FROM review_retries "
                                        "WHERE json_extract(payload,'$.last_attempt')=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("Attempt is not in the recovery queue; inspect learning_status after discovery")
            value = json.loads(row["payload"])
            can_close = (value["state"] == "blocked" and value["reason"] in {"unconfirmed_outcome", REVIEW_DEADLINE}
                         and not value.get("child_intent") and value.get("next_retry_at") is None)
            applied = can_close and not dry_run
            if applied:
                value.update(state="closed_without_retry", disposition={"at": self.clock(), "note": note, "evidence": evidence})
                self.store.db.execute("UPDATE review_retries SET state=?,payload=?,updated=? WHERE id=?",
                                      (value["state"], dumps(value), self.clock(), row["id"]))
        return {"attempt_id": attempt_id, "state": value["state"], "reason": value["reason"],
                "can_close_without_retry": can_close, "applied": applied, "disposition": value.get("disposition")}

    def process(self, api):
        if not self.settings.review_retry_enabled or not callable(getattr(api, "retry_review", None)):
            return []
        dispatched = []
        rows = self.store.db.execute("SELECT id,payload FROM review_retries WHERE state='waiting' AND due<=? "
                                     "ORDER BY due,id LIMIT 5", (self.clock(),)).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            try:
                result = api.retry_review(value["handle"], value["parent_intent"],
                                          failed_attempt_id=value["last_attempt"],
                                          max_retries=self.settings.review_retry_limit)
            except ValueError as exc:
                value.update(state="blocked", reason="native_rejected", error=scrub(str(exc))[:1000], next_retry_at=None)
                self.save(row["id"], value)
                continue
            value.update(state=result["status"], child_intent=result.get("intent_id"), next_retry_at=None)
            self.save(row["id"], value)
            if result["status"] == "queued":
                dispatched.append({**result, "root_intent": row["id"]})
        return dispatched

    def summary(self):
        counts = dict(self.store.db.execute("SELECT state,COUNT(*) FROM review_retries GROUP BY state"))
        rows = self.store.db.execute("SELECT payload FROM review_retries ORDER BY "
                                     "CASE state WHEN 'waiting' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
                                     "due,updated DESC LIMIT 20").fetchall()
        return {"enabled": self.settings.review_retry_enabled, "max_retries": self.settings.review_retry_limit,
                "counts": counts, "items": [json.loads(row[0]) for row in rows],
                "items_limit": 20, "has_more": sum(counts.values()) > len(rows)}
