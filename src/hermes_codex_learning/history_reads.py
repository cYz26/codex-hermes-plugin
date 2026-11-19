"""Scoped read-only history search backed by Hermes native SessionDB FTS5."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings
from .store import redact

_FTS_TOKEN = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}")


def _sanitize_fts_query(query: str) -> str | None:
    tokens = _FTS_TOKEN.findall(query)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens[:12]))


def _allowed_handles(settings: Settings, store, project: str, requested_session: str | None) -> dict[str, dict]:
    from .enterprise import MODE, boundary, scope_boundary
    active = boundary(settings.data_policy)
    if settings.data_policy.get("mode") == MODE and not active:
        return {}
    rows = store.db.execute(
        "SELECT key,value FROM meta WHERE key LIKE 'scope:%' AND json_extract(value,'$.project')=? "
        "ORDER BY rowid DESC LIMIT 100",
        (project,),
    )
    handles: dict[str, dict] = {}
    for key, raw in rows:
        handle = key[len("scope:"):]
        if requested_session and handle != requested_session:
            continue
        required = scope_boundary(store, handle)
        if required and (not active or required != active["id"]):
            continue
        handles[handle] = json.loads(raw)
    return handles


def search_history(settings: Settings, store, project: str | None, *, query: str = "",
                   session_id: str | None = None, limit: int = 10,
                   role_filter: str = "user,assistant") -> dict:
    if project is None:
        return {"success": True, "results": [], "reason": "workspace_required"}
    if not isinstance(query, str) or len(query) > 1000:
        raise ValueError("Invalid history query")
    if type(limit) is not int:
        raise ValueError("Invalid history limit")
    limit = max(1, min(limit, 20))
    roles = [role for role in (role_filter or "user,assistant").split(",") if role]
    role_set = set(roles)
    if not role_set or not role_set <= {"user", "assistant"}:
        raise ValueError("History search returns user and assistant excerpts")
    path = settings.home / "state.db"
    if not path.is_file():
        return {"success": True, "results": []}
    allowed = _allowed_handles(settings, store, project, session_id)
    if not allowed:
        return {"success": True, "results": []}

    from .enterprise import MODE, scrub
    sanitize = scrub if settings.data_policy.get("mode") == MODE else redact
    from hermes_state import SessionDB

    term = query.strip()
    db = SessionDB(db_path=path, read_only=True)
    try:
        results = []
        seen_handles = set()
        if term and hasattr(db, "search_messages"):
            fts_hits = []
            for candidate_query in dict.fromkeys(filter(None, [term, _sanitize_fts_query(term)])):
                try:
                    fts_hits = db.search_messages(
                        query=candidate_query,
                        role_filter=sorted(role_set),
                        exclude_sources=[],
                        limit=max(limit * 10, 60),
                        offset=0,
                    ) or []
                except Exception:
                    fts_hits = []
                fts_hits = [row for row in fts_hits if row.get("session_id") in allowed]
                if fts_hits:
                    break
            grouped: dict[str, list[dict]] = {}
            for hit in fts_hits:
                sid = hit["session_id"]
                if sid not in grouped and len(grouped) >= limit:
                    continue
                bucket = grouped.setdefault(sid, [])
                if len(bucket) >= 6:
                    continue
                body = str(hit.get("content") or hit.get("snippet") or "")
                lower_term = term.casefold()
                offset = max(0, body.casefold().find(lower_term)) if lower_term in body.casefold() else 0
                bucket.append({
                    "role": hit.get("role", "assistant"),
                    "content": sanitize(body[max(0, offset - 160):offset + 500]),
                    "timestamp": hit.get("timestamp"),
                })
            for sid, matches in grouped.items():
                seen_handles.add(sid)
                results.append({
                    "session_id": sid,
                    "source": allowed[sid].get("executor", "Codex App"),
                    "messages": matches,
                    "project": project,
                    "engine": "native_fts5",
                })
                if len(results) >= limit:
                    return {"success": True, "results": results}

        folded = term.casefold()
        tokens = [tok.casefold() for tok in _FTS_TOKEN.findall(term)]
        for handle, source in allowed.items():
            if handle in seen_handles:
                continue
            matches = []
            for message in reversed(db.get_messages(handle, include_ancestors=False)):
                body = message.get("content")
                if message.get("role") not in role_set or not isinstance(body, str):
                    continue
                low = body.casefold()
                matched = (not folded) or (folded in low) or (bool(tokens) and any(tok in low for tok in tokens))
                if not matched:
                    continue
                offset = low.find(folded) if folded and folded in low else (
                    next((low.find(tok) for tok in tokens if tok in low), 0))
                matches.append({
                    "role": message["role"],
                    "content": sanitize(body[max(0, offset - 160):offset + 500]),
                    "timestamp": message.get("timestamp"),
                })
                if len(matches) >= 6:
                    break
            if matches:
                results.append({
                    "session_id": handle,
                    "source": source.get("executor", "Codex App"),
                    "messages": list(reversed(matches)),
                    "project": project,
                })
            if len(results) >= limit:
                break
        return {"success": True, "results": results}
    finally:
        db.close()
