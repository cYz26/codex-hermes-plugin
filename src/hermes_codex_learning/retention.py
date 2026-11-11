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


def run(settings, store, api, *, dry_run=True):
    cutoff = time.time() - 30 * 86400
    result = {"dry_run":dry_run, "eligible_turns":0, "retired_turns":0}
    if not hasattr(api, "prune"):
        result.update(state="native_retention_unavailable")
        if not dry_run:
            store.set("retention:last",{**result,"at":time.time()})
        return result
    result["native"] = api.prune(retention_seconds=30*86400, dry_run=dry_run)
    from .learning_requests import retire
    result["explicit_learning"] = retire(store, cutoff, dry_run=dry_run)
    rows = store.db.execute("SELECT id,payload FROM turns WHERE activity<? AND observed_revision=revision "
                            "AND json_extract(payload,'$.status') IN ('completed','interrupted') ORDER BY activity LIMIT 1000",
                            (cutoff,)).fetchall()
    for row in rows:
        data = json.loads(row["payload"])
        source = {key:data["source"][key] for key in ("host","project","chat")}
        handles = store.get("chat-handles:"+digest(source), [])
        if not handles:
            continue
        eligible = set(result["native"].get("eligible_handles", [])) if dry_run else set()
        if not all(handle in eligible or api.read(kind="retention_state", name=handle).get("retired") for handle in handles):
            continue
        result["eligible_turns"] += 1
        if dry_run:
            continue
        with store.db:
            events = store.db.execute("SELECT id,source_key,created FROM events WHERE delivered IS NOT NULL "
                                      "AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.turn_id')=?",
                                      (source["chat"],data["source"]["turn"])).fetchall()
            for event in events:
                if event["source_key"]:
                    store.db.execute("INSERT OR IGNORE INTO retired_events VALUES (?,?,?)",
                                     (event["source_key"],event["id"],event["created"]))
                store.db.execute("DELETE FROM events WHERE id=?", (event["id"],))
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
        proofs=store.get("semantic-skill-attestations",{})
        changed=False
        for proof in proofs.values():
            evidence=proof.get("learning_evidence",{})
            if proof.get("review_handle") in handles and evidence.get("claims"):
                proof["learning_evidence"]={"state":evidence.get("state"),"proof_hash":digest(evidence),
                                            "retired_source_quotes":True,"review_input":evidence.get("review_input")}
                changed=True
        if changed:store.set("semantic-skill-attestations",proofs)
        result["retired_turns"] += 1
    queue = NoticeQueue(settings.state)
    try:
        result["notices"] = queue.retire(before=time.time()-90*86400,dry_run=dry_run)
    finally:
        queue.close()
    if not dry_run:
        with store.db:
            store.db.execute("DELETE FROM delivery_events WHERE delivered IS NOT NULL AND delivered<?", (cutoff,))
    result["runtime"] = runtime_cleanup(settings,dry_run=dry_run)
    if not dry_run:
        store.set("retention:last", {**result, "at":time.time()})
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
