"""Version-bound operator classification; the worker still owns publication.

For an explicitly reviewed personal-library candidate with legacy admission only.
Never overrides enterprise/source policies, resource checks or host validation.
"""
from __future__ import annotations

import time
from pathlib import Path

from .policy import sharing_check
from .store import digest


def matches(review, value, origins):
    return bool(review and review.get('decision') == 'approve_generic'
                and review.get('version') == value.get('version')
                and review.get('files') == value.get('files')
                and review.get('sources') == sorted(origins))


def submit(settings, store, project, review, *, dry_run=True):
    if settings.data_policy:
        raise ValueError('Use the configured data assessment policy for this deployment')
    if not isinstance(review, dict) or set(review) != {'name', 'version', 'files', 'sources', 'decision', 'note', 'evidence'}:
        raise ValueError('Provide name, version, full files manifest, sources, decision, note and evidence')
    if any(not isinstance(review.get(key), str) or not review[key].strip() or len(review[key]) > 4000
           for key in ('name', 'version', 'note', 'evidence')):
        raise ValueError('Review identity, note and evidence must be nonempty bounded text')
    value = store.get('artifact-read-cache', {}).get(review['name'], {}).get('artifact', {})
    origins = store.get('knowledge-index-v1', {}).get('skills', {}).get(review['name'], {}).get('projects', [])
    if project not in origins or not matches(review, value, origins):
        raise ValueError('Candidate version, files or source projects changed; review the current candidate')
    if not Path(value['resource_dir']).resolve().is_relative_to((settings.home / 'learning/artifacts').resolve()):
        raise ValueError('Candidate is outside the native artifact directory')
    check = sharing_check(value, origins, [], executable_candidate=settings.quality_checks)
    if not check['allowed']:
        raise ValueError('Candidate failed sharing checks: ' + check['reason'])
    result = {'state': 'preview' if dry_run else 'queued', 'name': review['name'], 'version': review['version'],
              'review_hash': digest(review), 'publication': 'not_inferred'}
    if not dry_run:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            approvals = store.get('publication-reviews-v1', {})
            approvals[review['name']] = {**review, 'review_hash': result['review_hash'], 'at': time.time(), 'project': project}
            store.set('publication-review:' + result['review_hash'], approvals[review['name']])
            store.set('publication-reviews-v1', approvals)
    return result
