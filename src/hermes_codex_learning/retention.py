"""Tiered evidence retention. Knowledge and indeterminate work remain protected."""
from __future__ import annotations

import json
import time
import os
import re
import shutil
from pathlib import Path

from .store import digest, dumps
from .notices import NoticeQueue


BATCH_SIZE = 1000
VACUUM_PAGES = 4096
_TURN_EVENTS = """json_extract(e.payload,'$.session_id')=json_extract(t.payload,'$.source.chat')
    AND json_extract(e.payload,'$.turn_id')=json_extract(t.payload,'$.source.turn')"""
_SETTLED = f"""t.activity<? AND t.observed_revision=t.revision
    AND json_extract(t.payload,'$.status') IN ('completed','interrupted')
    AND instr(COALESCE(json_extract(t.payload,'$.coverage'),''),'background_exit_pending')=0
    AND NOT EXISTS (SELECT 1 FROM events e WHERE {_TURN_EVENTS} AND e.delivered IS NULL)"""


def _prune_delivered_events(store, chat, turn, *, dry_run):
    rows = store.db.execute("""
        SELECT rowid,id,source_key,created FROM events WHERE delivered IS NOT NULL
          AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.turn_id')=?
    """, (chat, turn)).fetchall()
    if rows and not dry_run:
        # Commit the cursor floor with the deletion. New writers allocate above it.
        floor = max(store.get("events-rowid-floor", 0), *(row["rowid"] for row in rows))
        store.db.execute("INSERT OR REPLACE INTO meta VALUES ('events-rowid-floor',?)", (dumps(floor),))
        store.db.executemany("INSERT OR IGNORE INTO retired_events VALUES (?,?,?)",
                             ((row["source_key"], row["id"], row["created"]) for row in rows if row["source_key"]))
        store.db.executemany("DELETE FROM events WHERE id=?", ((row["id"],) for row in rows))
    return len(rows)


def run(settings, store, api, *, dry_run=True):
    cutoff = time.time() - 30 * 86400
    raw_cutoff = time.time() - settings.raw_event_retention_days * 86400
    result = {"dry_run":dry_run, "eligible_turns":0, "retired_turns":0,
              "eligible_raw_events": 0, "pruned_raw_events": 0, "pruned_transcript_cursors": 0,
              "pruned_with_retired_turns": 0}
    if not hasattr(api, "prune"):
        result.update(state="native_retention_unavailable")
        if not dry_run:
            store.set("retention:last",{**result,"at":time.time()})
        return result
    result["native"] = api.prune(retention_seconds=30*86400, dry_run=dry_run)
    from .learning_requests import retire
    result["explicit_learning"] = retire(store, cutoff, dry_run=dry_run)
    # Trim delivered raw events and settled transcript cursors once their turn is observed and older than raw_cutoff.
    settled_rows = store.db.execute(f"""
        SELECT t.id, json_extract(t.payload,'$.source.chat') chat, json_extract(t.payload,'$.source.turn') turn
        FROM turns t WHERE {_SETTLED}
          AND EXISTS (SELECT 1 FROM events e WHERE {_TURN_EVENTS} AND e.delivered IS NOT NULL)
        ORDER BY t.activity,t.id LIMIT ?
    """, (raw_cutoff, BATCH_SIZE + 1)).fetchall()
    result["more_raw_candidates"] = len(settled_rows) > BATCH_SIZE
    for row in settled_rows[:BATCH_SIZE]:
        chat, turn = row["chat"], row["turn"]
        with store.db:
            if not dry_run:
                store.db.execute("BEGIN IMMEDIATE")
                if not store.db.execute(f"SELECT 1 FROM turns t WHERE t.id=? AND {_SETTLED}",
                                        (row["id"], raw_cutoff)).fetchone():
                    continue
            pruned = _prune_delivered_events(store, chat, turn, dry_run=dry_run)
            result["eligible_raw_events"] += pruned
            if dry_run:
                continue
            result["pruned_raw_events"] += pruned
            cursor_keys = [
                "transcript-cursor:" + digest([chat, turn]),
                "transcript-cursor:" + digest([chat, turn, True]),
                "transcript-cursor:" + digest([chat, turn, False]),
            ]
            for ck in cursor_keys:
                deleted = store.db.execute("DELETE FROM meta WHERE key=?", (ck,)).rowcount
                result["pruned_transcript_cursors"] += deleted
    cursor = store.get("retention:turn-cursor") or [0, ""]
    rows = store.db.execute(f"""
        SELECT t.id,t.payload,t.activity FROM turns t WHERE {_SETTLED} AND (t.activity,t.id)>(?,?)
        ORDER BY t.activity,t.id LIMIT ?
    """, (cutoff, *cursor, BATCH_SIZE + 1)).fetchall()
    result["more_turn_candidates"] = len(rows) > BATCH_SIZE
    rows = rows[:BATCH_SIZE]
    eligible_handles = set(result["native"].get("eligible_handles", [])) if dry_run else set()
    retired_handles = {}
    for row in rows:
        data = json.loads(row["payload"])
        source = {key:data["source"][key] for key in ("host","project","chat")}
        handles = store.get("chat-handles:"+digest(source), [])
        if not handles:
            continue
        for handle in handles:
            if handle not in retired_handles:
                retired_handles[handle] = handle in eligible_handles or api.read(kind="retention_state", name=handle).get("retired")
        if not all(retired_handles[handle] for handle in handles):
            continue
        result["eligible_turns"] += 1
        if dry_run:
            continue
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            if not store.db.execute(f"SELECT 1 FROM turns t WHERE t.id=? AND {_SETTLED}",
                                    (row["id"], cutoff)).fetchone():
                continue
            result["pruned_with_retired_turns"] += _prune_delivered_events(
                store, source["chat"], data["source"]["turn"], dry_run=False)
            retired = {"source":data["source"], "user":"", "assistant":"", "tools":{}, "status":"retired",
                       "coverage":["body_retired_after_30_days"]}
            store.db.execute("UPDATE turns SET payload=? WHERE id=?", (dumps(retired),row["id"]))
            store.db.execute("DELETE FROM learning_signals WHERE chat=? AND turn=?", (source["chat"],data["source"]["turn"]))
            store.db.execute("DELETE FROM review_priority WHERE id=?", (row["id"],))
            store.db.execute("DELETE FROM meta WHERE key=?", ("transcript-cursor:"+digest([source["chat"],data["source"]["turn"]]),))
            for mode in (True,False):
                store.db.execute("DELETE FROM meta WHERE key=?",("transcript-cursor:"+digest([source["chat"],data["source"]["turn"],mode]),))
            for prefix in ("observation-receipt:","turn-admission:","input-retry:","capture-refusal:","recall-offer:"):
                store.db.execute("DELETE FROM meta WHERE key=?",(prefix+row["id"],))
            for handle in handles:
                progress=store.get("review-progress:"+handle,{})
                prior_inputs=store.db.execute("SELECT key FROM meta WHERE key LIKE 'review-input:%' AND json_extract(value,'$.handle')=?",(handle,)).fetchall()
                for item in prior_inputs:
                    intent_id=item[0][len("review-input:"):]
                    for prefix in ("review-input:","review-candidates:"):
                        store.db.execute("DELETE FROM meta WHERE key=?",(prefix+intent_id,))
                if progress.get("intent"):
                    for prefix in ("review-input:","review-candidates:"):
                        store.db.execute("DELETE FROM meta WHERE key=?",(prefix+progress["intent"],))
                store.db.execute("DELETE FROM meta WHERE key=?",("review-progress:"+handle,))
        for key in ("semantic-skill-attestations", "archived-skill-origins"):
            records = store.get(key, {})
            proofs = records.values() if key == "semantic-skill-attestations" else (
                record.get("attestation", {}) for record in records.values())
            changed = False
            for proof in proofs:
                evidence = proof.get("learning_evidence", {})
                if proof.get("review_handle") in handles and evidence.get("claims"):
                    proof["learning_evidence"] = {"state":evidence.get("state"), "proof_hash":digest(evidence),
                        "retired_source_quotes":True, "review_input":evidence.get("review_input")}
                    changed = True
            if changed:
                store.set(key, records)
        result["retired_turns"] += 1
    if not dry_run:
        last = rows[-1] if result["more_turn_candidates"] else None
        store.set("retention:turn-cursor", [last["activity"], last["id"]] if last else None)
    queue = NoticeQueue(settings.state)
    try:
        result["notices"] = queue.retire(before=time.time()-90*86400,dry_run=dry_run)
    finally:
        queue.close()
    if not dry_run:
        with store.db:
            store.db.execute("DELETE FROM delivery_events WHERE delivered IS NOT NULL AND delivered<?", (cutoff,))
    incremental = store.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    reusable = store.db.execute("PRAGMA freelist_count").fetchone()[0]
    if not dry_run and incremental and reusable:
        store.db.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})").fetchall()
    if not dry_run and (result["pruned_raw_events"] or result["retired_turns"] or (incremental and reusable)):
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    page_size = store.db.execute("PRAGMA page_size").fetchone()[0]
    result["database"] = {
        "allocated_bytes": page_size * store.db.execute("PRAGMA page_count").fetchone()[0],
        "reusable_bytes": page_size * store.db.execute("PRAGMA freelist_count").fetchone()[0],
        "incremental_vacuum": incremental,
    }
    result["runtime"] = runtime_cleanup(settings,dry_run=dry_run)
    result['artifacts'] = retain_artifacts(store, api, dry_run=dry_run)
    if not dry_run:
        store.set("retention:last", {**result, "at":time.time()})
    return result


def retain_artifacts(store, api, *, dry_run=True):
    """Collect only unreferenced host snapshots through their native owner.

    Native archives, the mutation ledger and its blobs keep their own policies.
    The 30-day grace also covers stateless read receipts not yet acknowledged.
    """
    if not hasattr(api, 'prune_artifacts'):
        return {'state': 'native_artifact_retention_unavailable', 'dry_run': dry_run}
    protected = set()
    def protect(record):
        version = record.get('artifact', record).get('version')
        if isinstance(version, str) and re.fullmatch(r'[a-f0-9]{64}', version):
            protected.add(version)
    lineage = {}
    def remember_lineage(record):
        review = record.get('check', {}).get('automatic_review')
        if review:
            lineage[record['artifact']['version']] = (review['baseline_version'], review['previous_version'])
    for key in ('publications-v1', 'artifact-read-cache'):
        for record in store.get(key, {}).values():
            protect(record)
            remember_lineage(record)
    now = time.time()
    retired = store.get('artifact-retired-at', {})
    protected.update(version for version, at in retired.items() if at >= now-30*86400)
    histories = {}
    for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'publication-history:%'"):
        history = json.loads(row['value'])
        for position, (version, record) in enumerate(sorted(history.items(),
                key=lambda item: item[1].get('retired_at', now), reverse=True)):
            # Legacy undated versions get a grace period before collection.
            record.setdefault('retired_at', now)
            remember_lineage(record)
            if position < 2 or record['retired_at'] >= now-30*86400:
                protect(record)
        histories[row['key']] = history
    for row in store.db.execute("SELECT payload FROM application_evidence"):
        protect(json.loads(row[0]))
    for row in store.db.execute("SELECT value FROM meta WHERE key LIKE 'issued-delivery:%'"):
        protect(json.loads(row[0]))
    # Published, retained or delivered candidates keep the source versions they reference.
    candidates = {intent.get('version'): intent.get('sources', {}).values()
                  for intent in store.get('native-candidate-intents', {}).values()}
    frontier = list(protected)
    while frontier:
        current = frontier.pop()
        dependencies = (*candidates.get(current, ()), *lineage.get(current, ()))
        for version in dependencies:
            if isinstance(version, str) and version not in protected and re.fullmatch(r'[a-f0-9]{64}', version):
                protected.add(version)
                frontier.append(version)
    result = api.prune_artifacts(sorted(protected), retention_seconds=30*86400, dry_run=dry_run)
    if not dry_run:
        removed = set(result.get('removed', []))
        with store.db:
            for key, history in histories.items():
                store.db.execute('UPDATE meta SET value=? WHERE key=?',
                                 (dumps({v:r for v,r in history.items() if v not in removed}), key))
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                             ('artifact-retired-at', dumps({v:at for v,at in retired.items() if v not in removed})))
    return result


def runtime_cleanup(settings, *, dry_run=True):
    from .updates import exclusive, read_json, UpdateError
    directory = settings.home.parent/"updates"/digest(str(settings.home))[:16]
    result = {"dry_run":dry_run,"eligible_candidates":0,"removed_candidates":0,"eligible_snapshots":0,
              "removed_snapshots":0,"disk_free_bytes":shutil.disk_usage(settings.home.parent).free}
    protected = [settings.source, settings.python, settings.fallback_python, settings.official_source]
    try:
        with exclusive(directory/"update.lock"):
            for name in ("previous.json","candidate.json"):
                record = read_json(directory/name,{})
                protected.extend(Path(record[key]) for key in ("source","python","fallback_python") if record.get(key))
            transaction = read_json(directory/"transaction.json",{})
            active_snapshot = None
            if transaction.get("state") in {"switching","starting","restoring"}:
                active_snapshot = transaction.get("snapshot")
                for state in ("before","after"):
                    protected.extend(Path(transaction.get(state,{}).get(key)) for key in ("source","python","fallback_python")
                                     if transaction.get(state,{}).get(key))
            protected = [path.absolute() for path in protected if path is not None]
            gateway_command = None
            if settings.site_manifest:
                site = read_json(settings.site_manifest.parent/"current-site.json",{})
                if site.get("gateway_home"):
                    state = read_json(Path(site["gateway_home"])/"gateway_state.json",{})
                    pid = state.get("pid")
                    proc = Path("/proc")/str(pid)
                    if type(pid) is int and pid > 1 and proc.exists() and proc.stat().st_uid == os.getuid():
                        gateway_command = (proc/"cmdline").read_bytes()
            cutoff = time.time()-30*86400
            for folder in (directory/"candidates").glob("v*"):
                if folder.is_symlink() or not re.fullmatch(r"v\d+\.\d+\.\d+-[a-f0-9]{12}",folder.name):
                    continue
                record = read_json(folder/"verification.json",{})
                if (record.get("state") not in {"ready","failed","failed_activation"}
                        or record.get("source") != str(folder/"source")
                        or record.get("started_at",time.time()) >= cutoff
                        or any(path.is_relative_to(folder) for path in protected)):
                    continue
                if record.get("site") and (gateway_command is None or str(folder).encode() in gateway_command):
                    continue
                result["eligible_candidates"] += 1
                if not dry_run:
                    shutil.rmtree(folder)
                    result["removed_candidates"] += 1
            for folder in (directory/"snapshots").glob("*"):
                if folder.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}",folder.name) or str(folder) == active_snapshot:
                    continue
                complete = folder/"complete.json"
                if (not complete.is_file() or complete.stat().st_mtime >= cutoff
                        or read_json(complete,{}).get("home") != str(settings.home)):
                    continue
                result["eligible_snapshots"] += 1
                if not dry_run:
                    shutil.rmtree(folder)
                    result["removed_snapshots"] += 1
    except (OSError,ValueError,UpdateError):
        result["state"] = "deferred"
    return result
