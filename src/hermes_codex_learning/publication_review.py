"""Project-scoped review of exact personal-library publication candidates."""
from __future__ import annotations

import time
from pathlib import Path

from .policy import published_resource, sharing_check
from .store import digest, dumps

UNASSESSED = 'data_classification_unassessed'
APPROVE = 'approve_generic'
KEEP = 'keep_project'
KEPT_REASON = 'operator_kept_in_project'
STALE = '候选版本、文件或来源已变化，请刷新后重新审查。'


def identity(name, value, origins):
    return {'name': name, 'version': value['version'], 'files': value['files'], 'sources': sorted(origins)}


def disposition(review, candidate):
    if review and all(review.get(key) == value for key, value in candidate.items()):
        return review.get('decision')
    return None


def enabled(settings):
    return settings.checked_publication and not settings.data_policy


def pending(settings, store, project, *, name=None):
    if not enabled(settings):
        return
    decisions = store.get('publication-decisions', {})
    sources = store.get('knowledge-index-v1', {}).get('skills', {})
    artifacts = store.get('artifact-read-cache', {})
    publications = store.get('publications-v1', {})
    reviews = store.get('publication-reviews-v1', {})
    for skill in [name] if name is not None else sorted(decisions):
        decision = decisions.get(skill, {})
        origins = sources.get(skill, {}).get('projects', [])
        value = artifacts.get(skill, {}).get('artifact')
        if (origins != [project] or not value or decision.get('reason') != UNASSESSED
                or not decision.get('review_candidate')):
            continue
        candidate = identity(skill, value, origins)
        previous = publications.get(skill, {}).get('artifact', {})
        if (decision['review_candidate'] != digest(candidate) or previous.get('version') == value['version']
                or disposition(reviews.get(skill), candidate)):
            continue
        revision = digest([candidate, previous.get('version'), previous.get('files')])
        yield candidate, revision, value, previous


def list_pending(settings, store, project):
    if not enabled(settings):
        return {'candidates': [], 'decisions': []}
    candidates = []
    for candidate, revision, _, previous in pending(settings, store, project):
        files, old_files = candidate['files'], previous.get('files', {})
        resources = []
        for path in sorted(files.keys() | old_files.keys(), key=lambda path: (path != 'SKILL.md', path)):
            before, after = old_files.get(path), files.get(path)
            change = ('added' if before is None else 'removed' if after is None else
                      'unchanged' if before == after else 'changed')
            resources.append({'path': path, 'change': change, 'sha256': after})
        candidates.append({'name': candidate['name'], 'version': candidate['version'], 'revision': revision,
                           'sources': candidate['sources'], 'previous_version': previous.get('version'),
                           'resources': resources})
    publications = store.get('publications-v1', {})
    reviews = store.get('publication-reviews-v1', {})
    decisions = store.get('publication-decisions', {})
    recent = []
    for name, review in sorted(reviews.items(), key=lambda item: item[1]['at'], reverse=True):
        if review['project'] != project or review['sources'] != [project]:
            continue
        version = review['version']
        checked = decisions.get(name, {})
        reason = checked.get('reason')
        if review['decision'] == KEEP:
            state = 'kept_in_project'
        elif publications.get(name, {}).get('artifact', {}).get('version') == version:
            state = 'published'
        elif checked.get('version') == version and reason not in (None, UNASSESSED):
            state = 'withheld'
        else:
            state = 'queued' if checked.get('version') == version and reason == UNASSESSED else 'superseded'
        recent.append({'name': name, 'version': version, 'state': state, 'at': review['at'],
                       **({'reason': reason} if state == 'withheld' else {})})
        if len(recent) == 10:
            break
    return {'candidates': candidates, 'decisions': recent}


def current_candidate(settings, store, project, name, revision):
    if not isinstance(name, str) or not name or not isinstance(revision, str):
        raise ValueError(STALE)
    item = next(pending(settings, store, project, name=name), None)
    if item is None or item[1] != revision:
        raise ValueError(STALE)
    return item


def preview_file(settings, store, project, name, revision, path):
    _, _, value, previous = current_candidate(settings, store, project, name, revision)
    if not isinstance(path, str) or not path or path not in value['files'].keys() | previous.get('files', {}).keys():
        raise ValueError('这个文件不在待审清单中。')
    try:
        after = published_resource({'artifact': value}, path)['content'] if path in value['files'] else None
    except (OSError, ValueError) as exc:
        raise ValueError(STALE) from exc
    before = None
    if path in previous.get('files', {}):
        try:
            before = published_resource({'artifact': previous}, path)['content']
        except (OSError, ValueError):
            return {'before': None, 'after': after, 'previous_unavailable': True}
    return {'before': before, 'after': after}


def verify_candidate(settings, store, name, value, origins):
    if not Path(value['resource_dir']).resolve().is_relative_to((settings.home / 'learning/artifacts').resolve()):
        raise ValueError('Candidate is outside the native artifact directory')
    from .enterprise import boundary as enterprise_boundary, is_trusted_local
    trusted_local = is_trusted_local(settings.data_policy)
    enterprise = enterprise_boundary(settings.data_policy)
    intent = store.get('native-candidate-intents', {}).get(name, {})
    native_review_candidate = trusted_local or (settings.review_protocol == 'native' and bool(enterprise))
    check = sharing_check(value, origins, [], executable_candidate=settings.quality_checks,
                          enterprise=bool(enterprise or trusted_local),
                          native_candidate=native_review_candidate or (intent.get('version') == value['version'] and bool(intent.get('sources'))),
                          allow_absolute_paths=trusted_local)
    if not check['allowed']:
        raise ValueError('Candidate failed sharing checks: ' + check['reason'])


def record(store, project, review, *, dry_run=False):
    approvals = store.get('publication-reviews-v1', {})
    name, version = review['name'], review['version']
    saved = approvals.get(name)
    decision = disposition(saved, {key: review[key] for key in ('name', 'version', 'files', 'sources')})
    if decision and decision != review['decision']:
        raise ValueError('这个版本已处理，请刷新查看结果。')
    review_hash = saved['review_hash'] if decision else digest(review)
    state = 'preview' if dry_run else 'queued' if review['decision'] == APPROVE else 'kept_in_project'
    if not dry_run and not decision:
        approvals[name] = {**review, 'review_hash': review_hash, 'at': time.time(), 'project': project}
        store.db.executemany('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             [('publication-review:' + review_hash, dumps(approvals[name])),
                              ('publication-reviews-v1', dumps(approvals))])
    return {'state': state, 'name': name, 'version': version, 'review_hash': review_hash,
            'publication': 'not_inferred'}


def decide(settings, store, project, name, revision, decision, read_current):
    if decision not in (APPROVE, KEEP):
        raise ValueError('需要明确批准共享或保留在本项目。')
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        candidate, _, _, _ = current_candidate(settings, store, project, name, revision)
        value = read_current(name)
        if identity(name, value, candidate['sources']) != candidate:
            raise ValueError(STALE)
        verify_candidate(settings, store, name, value, candidate['sources'])
        if (store.get('lifecycle-withheld', {}).get(name) or store.db.execute(
                'SELECT 1 FROM withheld_versions WHERE name=? AND version=?', (name, candidate['version'])).fetchone()):
            raise ValueError(STALE)
        return record(store, project, {**candidate, 'decision': decision,
                                        'note': 'Operator decision in scoped publication review',
                                        'evidence': 'desktop-publication-review:' + revision})


def submit(settings, store, project, review, *, dry_run=True):
    if settings.data_policy:
        raise ValueError('Use the configured data assessment policy for this deployment')
    if not isinstance(review, dict) or set(review) != {'name', 'version', 'files', 'sources', 'decision', 'note', 'evidence'}:
        raise ValueError('Provide name, version, full files manifest, sources, decision, note and evidence')
    if any(not isinstance(review.get(key), str) or not review[key].strip() or len(review[key]) > 4000
           for key in ('name', 'version', 'note', 'evidence')):
        raise ValueError('Review identity, note and evidence must be nonempty bounded text')
    if review['decision'] != APPROVE:
        raise ValueError('Use an explicit approval for the operator classification file')
    with store.db:
        if not dry_run:
            store.db.execute('BEGIN IMMEDIATE')
        value = store.get('artifact-read-cache', {}).get(review['name'], {}).get('artifact')
        origins = store.get('knowledge-index-v1', {}).get('skills', {}).get(review['name'], {}).get('projects', [])
        if project not in origins or not value or not disposition(review, identity(review['name'], value, origins)):
            raise ValueError('Candidate version, files or source projects changed; review the current candidate')
        verify_candidate(settings, store, review['name'], value, origins)
        return record(store, project, review, dry_run=dry_run)
