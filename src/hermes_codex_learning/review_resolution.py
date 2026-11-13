"""Queue an explicit, exact-version inspection for the native serial owner."""
from __future__ import annotations

import json
import time

from .learning_requests import get
from .store import digest


def submit(store, project, inspection, *, dry_run=True):
    required = {'request_id', 'attempt_id', 'outcome_hash', 'artifact_versions', 'note', 'evidence'}
    if not isinstance(inspection, dict) or set(inspection) != required:
        raise ValueError('Provide the request, attempt, native outcome hash, all artifact versions, note and evidence')
    if any(not isinstance(inspection[k], str) or not 0 < len(inspection[k].strip()) <= 2000
           for k in required - {'artifact_versions'}):
        raise ValueError('Inspection identity, note and evidence must be bounded nonempty text')
    value = get(store, project, inspection['request_id'])
    results = value.get('results') or []
    if value['state'] != 'unknown' or not results or results[-1]['id'] != inspection['attempt_id']:
        raise ValueError('Inspect the exact unknown request attempt in its original project')
    last = results[-1]
    versions = inspection['artifact_versions']
    if not isinstance(versions, dict) or not versions or versions != last.get('artifact_versions'):
        raise ValueError('Inspection must cover every artifact version recorded by the attempt')
    cache = store.get('artifact-read-cache', {})
    if any(cache.get(name, {}).get('artifact', {}).get('version') != version for name, version in versions.items()):
        raise ValueError('Affected artifacts changed; inspect their current state before resolving')
    identity = digest([project, inspection])
    prior = store.get('review-inspection:' + identity)
    if prior:
        return prior
    result = {'state': 'preview' if dry_run else 'queued', 'inspection_id': identity,
              'project': project, 'handle': value['handle'], 'intent_id': value['intent_id'],
              'inspection': inspection, 'at': time.time()}
    if not dry_run:
        store.set('review-inspection:' + identity, result)
    return result


def process(bridge):
    if not callable(getattr(bridge.api, 'close_review_without_retry', None)):
        return  # A rollback to an older native API must not break ordinary ingestion.
    rows = bridge.store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'review-inspection:%' "
                                   "AND json_extract(value,'$.state')='queued'").fetchall()
    for row in rows:
        value = json.loads(row['value'])
        inspection = {k: v for k, v in value['inspection'].items() if k != 'request_id'}
        try:
            result = bridge.api.close_review_without_retry(value['handle'], value['intent_id'], **inspection)
            value.update(state='closed_without_retry', native=result)
        except (ValueError, OSError) as exc:
            value.update(state='rejected', error=str(exc))
        bridge.store.set(row['key'], value)
