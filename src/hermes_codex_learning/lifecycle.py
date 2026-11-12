"""Project views of native skill mutations; never rewrite native knowledge or policy."""
from __future__ import annotations

import time
import json
from datetime import datetime, timezone
from pathlib import Path
from contextvars import ContextVar

from .store import dumps

_CURATION_SCOPE = ContextVar('native_curation_scope', default=None)


def curation_tool_guard(*, tool_name, args, **kwargs):
    scope = _CURATION_SCOPE.get()
    if scope is None:
        return None
    denied = {'action': 'block', 'message': 'Use only the supplied, admitted native Curator skills; load them with skill_view.'}
    if not isinstance(args, dict):
        return denied
    if tool_name == 'skill_view':
        return None if args.get('name') in scope['owned'] else denied
    if tool_name == 'skills_list':
        return None if scope['present'] <= scope['owned'] else denied
    if tool_name != 'skill_manage':
        return denied
    operations = args.get('operations', [args])
    if not isinstance(operations, list):
        return denied
    owned = set(scope['owned'])
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get('name'), str):
            return denied
        if op.get('action') == 'create' and op['name'] not in scope['present']:
            owned.add(op['name'])
        if op['name'] not in owned:
            return denied
        if op.get('absorbed_into') and op['absorbed_into'] not in owned:
            return denied
    scope['owned'] = owned
    return None


def synchronize(bridge):
    store = bridge.store
    if not bridge.native_capabilities.get('skill_lifecycle'):
        return
    cursor = store.get('native-lifecycle-cursor')
    page = bridge.api.read(kind='skill_lifecycle', name=cursor)
    if not page.get('cursor_found', True):
        store.set('native-lifecycle-status', {'state': 'history_gap', 'at': time.time(),
                                             'reason': 'native_ledger_cursor_expired'})
        return  # Do not guess ancestry after the native ledger has been trimmed.
    index = store.get('knowledge-index-v1', {'memory': {}, 'skills': {}})
    skills = index.setdefault('skills', {})
    archived = store.get('archived-skill-origins', {})
    aliases = store.get('native-skill-aliases', {})
    published_aliases = store.get('publication-aliases', {})
    blocked = store.get('lifecycle-withheld', {})
    attestations = store.get('semantic-skill-attestations', {})
    publications = store.get('publications-v1', {})
    cache = store.get('artifact-read-cache', {})
    for event in page.get('events', []):
        name, action = event['skill'], event['action']
        target = event.get('absorbed_into')
        if action in {'archive', 'delete'}:
            if name in skills:
                archived[name] = {'origin': dict(skills[name]), 'attestation': attestations.get(name, {}),
                                  'version': cache.get(name, publications.get(name, {})).get('artifact', {}).get('version')}
            if target:
                origin = skills.get(name) or archived.get(name, {}).get('origin', {})
                if not origin.get('projects'):
                    blocked[target] = {'reason': 'unknown_merge_source', 'event': event['id']}
                    continue
                current = skills.setdefault(target, {'projects': []})
                current['projects'] = sorted(set(current['projects']) | set(origin['projects']))
                current['native_lineage'] = sorted(set(current.get('native_lineage', [])) | {name})
                boundaries = {value for value in (
                    attestations.get(name, archived.get(name, {}).get('attestation', {})).get('boundary_id'),
                    attestations.get(target, {}).get('boundary_id'),
                    publications.get(name, {}).get('check', {}).get('boundary_id'),
                    publications.get(target, {}).get('check', {}).get('boundary_id')) if value}
                if len(boundaries) > 1:
                    blocked[target] = {'reason': 'incompatible_enterprise_boundaries', 'event': event['id']}
                elif boundaries:
                    # Scope ancestry is not a validation certificate for the new content.
                    attestations[target] = {'boundary_id': boundaries.pop(), 'native_event': event['id']}
                aliases[name] = target
        elif action == 'restore':
            if name in archived:
                skills[name] = dict(archived[name]['origin'])
                if archived[name].get('attestation'):
                    attestations[name] = archived[name]['attestation']
            if name in aliases and published_aliases.get(name) == aliases[name]:
                published_aliases.pop(name, None)
            aliases.pop(name, None)
    values = {'knowledge-index-v1': index, 'archived-skill-origins': archived,
              'native-skill-aliases': aliases, 'lifecycle-withheld': blocked,
              'publication-aliases': published_aliases,
              'semantic-skill-attestations': attestations,
              'native-lifecycle-cursor': page.get('cursor', cursor),
              'native-lifecycle-status': {'state': 'catching_up' if page.get('has_more') else 'ready',
                                          'at': time.time(), 'events': len(page.get('events', [])),
                                          'withheld': blocked}}
    with store.db:
        for key, value in values.items():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, dumps(value)))
    return page


def aliases_for(store, publications):
    links = store.get('native-skill-aliases', {})
    result = {}
    for name in links:
        target, seen = name, set()
        while target in links and target not in seen:
            seen.add(target)
            target = links[target]
        if target not in seen and target in publications:
            result[name] = target
    return result


def recover_archived(bridge, context):
    """Native whole-library rollback may restore packages without restore events."""
    from .native_reads import skill
    store = bridge.store
    index = store.get('knowledge-index-v1', {'skills': {}})
    archived = store.get('archived-skill-origins', {})
    if not archived:
        return
    links = store.get('native-skill-aliases', {})
    published_links = store.get('publication-aliases', {})
    proofs = store.get('semantic-skill-attestations', {})
    for item in context.get('skills', []):
        name = item['name']
        prior = archived.get(name, {})
        if not prior.get('version') or (name in index.get('skills', {}) and name not in links):
            continue
        if skill(bridge.api, name=name)['version'] != prior['version']:
            continue
        index.setdefault('skills', {})[name] = prior['origin']
        if published_links.get(name) == links.get(name):
            published_links.pop(name, None)
        links.pop(name, None)
        if prior.get('attestation'):
            proofs[name] = prior['attestation']
    with store.db:
        for key, value in {'knowledge-index-v1': index, 'native-skill-aliases': links,
                           'publication-aliases': published_links, 'semantic-skill-attestations': proofs}.items():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, dumps(value)))


def maintenance(bridge, **kwargs):
    # Snapshot the native ledger cursor before any native writer runs.
    page = synchronize(bridge)
    if (page and page.get('has_more')) or bridge.store.get('native-lifecycle-status', {}).get('state') == 'history_gap':
        return {'curator': None, 'reason': 'native_lifecycle_reconciliation_required'}
    scope = None
    if bridge.native_capabilities.get('skill_lifecycle'):
        status = bridge.api.read(kind='curator_status')
        bridge.store.set('native-curator-status', status)
        last = status.get('last_run_at')
        due = (last and datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(last).timestamp()
               >= status['interval_hours'] * 3600)
        if status.get('enabled') and status.get('consolidate') and not status.get('paused') and due:
            reason = curation_admission(bridge, status.get('managed_skills', []))
            if reason:
                bridge.store.set('native-curator-status', {**status, 'deferred_reason': reason})
                return {'curator': None, 'reason': reason}
            if bridge.settings.data_policy:
                scope = {'owned': set(status.get('managed_skills', [])),
                         'inputs': set(status.get('managed_skills', [])),
                         'present': {s['name'] for s in bridge.api.read(kind='context').get('skills', [])}}
                scope['before'] = {s['name']: s['version'] for s in bridge.api.read(kind='context').get('skills', [])}
                from .native_reads import skill
                published = bridge.store.get('publications-v1', {})
                scope['source_versions'] = {name: skill(bridge.api, name=name)['version'] for name in scope['inputs']}
                scope['shareable'] = bool(scope['inputs']) and all(
                    published.get(name, {}).get('artifact', {}).get('version') == version
                    and published[name].get('check', {}).get('allowed')
                    for name, version in scope['source_versions'].items())
    bridge.store.set('state', 'maintaining')
    bridge.store.set('loop-progress', {'phase':'maintaining', 'scope':'native_library', 'updated_at':time.time()})
    if bridge.native_capabilities.get('skill_lifecycle'):
        activity = max((row['activity'] for row in bridge.latest_turns()), default=None)
        if activity is not None:
            kwargs['host_idle_seconds'] = max(0, time.time()-activity)
    token = _CURATION_SCOPE.set(scope)
    if scope:
        bridge.store.set('native-curator-checkpoint', {'before': scope['before'], 'at': time.time()})
    try:
        result = bridge.api.maintenance(**kwargs)
    finally:
        _CURATION_SCOPE.reset(token)
    if scope and result.get('curator'):
        inherit_curator_inputs(bridge, scope)
    if scope:
        bridge.store.set('native-curator-checkpoint', None)
    synchronize(bridge)
    if bridge.native_capabilities.get('skill_lifecycle'):
        bridge.store.set('native-curator-status', bridge.api.read(kind='curator_status'))
    return result


def inherit_curator_inputs(bridge, scope):
    """Conservative provenance: every admitted input may inform any revised output."""
    store = bridge.store
    index = store.get('knowledge-index-v1', {'skills': {}})
    # Only the original library is input evidence; newly created names are outputs.
    inputs = set(scope['before']) & scope['inputs']
    inputs &= set(index.get('skills', {}))
    origins = sorted({p for name in inputs for p in index['skills'][name].get('projects', [])})
    proofs = store.get('semantic-skill-attestations', {})
    publications = store.get('publications-v1', {})
    boundaries = {b for name in inputs for b in (proofs.get(name, {}).get('boundary_id'),
        publications.get(name, {}).get('check', {}).get('boundary_id')) if b}
    blocked = store.get('lifecycle-withheld', {})
    candidates = store.get('native-candidate-intents', {})
    for item in bridge.api.read(kind='context').get('skills', []):
        name = item['name']
        if item['version'] == scope['before'].get(name):
            continue
        if not origins or len(boundaries) > 1:
            blocked[name] = {'reason': 'curator_input_scope_unresolved'}
            continue
        index.setdefault('skills', {})[name] = {'projects': origins,
            'native_curator_inputs': sorted(inputs), 'basis': 'admitted_native_curator_inputs'}
        proofs[name] = {'boundary_id': next(iter(boundaries))} if boundaries else {}
        if scope.get('shareable'):
            from .native_reads import skill
            candidates[name] = {'version': skill(bridge.api, name=name)['version'],
                                'sources': scope['source_versions']}
    with store.db:
        for key, value in {'knowledge-index-v1': index, 'semantic-skill-attestations': proofs,
                           'lifecycle-withheld': blocked, 'native-candidate-intents': candidates}.items():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, dumps(value)))


def reconcile_interrupted_curation(bridge, context):
    checkpoint = bridge.store.get('native-curator-checkpoint')
    if not checkpoint:
        return
    blocked = bridge.store.get('lifecycle-withheld', {})
    for item in context.get('skills', []):
        if item.get('version') != checkpoint['before'].get(item['name']):
            blocked[item['name']] = {'reason': 'native_curator_result_unconfirmed', 'at': checkpoint['at']}
    bridge.store.set('lifecycle-withheld', blocked)
    bridge.store.set('native-curator-interrupted', checkpoint)
    bridge.store.set('native-curator-checkpoint', None)


def curation_admission(bridge, names):
    """Apply the existing content policy to the native library before an LLM sees it."""
    from .native_reads import skill
    from .policy import admit
    from .enterprise import boundary
    policy, store = bridge.settings.data_policy, bridge.store
    if not policy:
        return None
    approved = boundary(policy)
    index = store.get('knowledge-index-v1', {}).get('skills', {})
    proofs = store.get('semantic-skill-attestations', {})
    publications = store.get('publications-v1', {})
    for name in names:
        origins = index.get(name, {}).get('projects', [])
        if not origins:
            return 'curator_unknown_source'
        required = {value for value in (proofs.get(name, {}).get('boundary_id'),
                    publications.get(name, {}).get('check', {}).get('boundary_id')) if value}
        if required and (not approved or required != {approved['id']}):
            return 'curator_enterprise_boundary_not_active'
        value = skill(bridge.api, name=name)
        root = Path(value['resource_dir'])
        try:
            resources = {path: (root/path).read_text() for path in value['files']}
        except (OSError, UnicodeError):
            return 'curator_resource_not_assessed'
        for project in origins:
            payload = {'resources': resources}
            assessment = admit(payload, policy, project, purpose='curation')
            if not assessment.get('allowed'):
                return 'curator_' + assessment.get('reason', 'content_not_admitted')
            if assessment.get('payload', payload) != payload:
                return 'curator_resource_requires_redaction'
    return None


def configure(bridge):
    """Apply the explicit operator request on the write owner, preserving scope."""
    from .enterprise import MODE, boundary, make_boundary
    from .store import atomic_json
    policy, store = bridge.settings.data_policy, bridge.store
    request = store.get('native-curator-config-request', {})
    status = bridge.api.read(kind='curator_status')
    approved = boundary(policy)
    record = None
    if policy.get('mode') == MODE:
        try:
            record = json.loads(Path(policy['enterprise_boundary']).read_text())
        except (OSError, ValueError):
            pass
        if (not approved and request.get('authorized_boundary') and record == request['authorized_boundary']
                and request.get('configuration_scope_hash') == status.get('configuration_scope_hash')):
            approved = request['authorized_boundary']
    if policy.get('mode') == MODE and not approved:
        store.set('native-curator-config-request', None)
        store.set('native-curator-config-result', {'state': 'refused', 'reason': 'enterprise_boundary_not_active'})
        return
    if request.get('configuration_scope_hash') and request['configuration_scope_hash'] != status['configuration_scope_hash']:
        store.set('native-curator-config-result', {'state': 'refused', 'reason': 'configuration_changed'})
        store.set('native-curator-config-request', None)
        return
    store.set('native-curator-config-request', {**request, 'authorized_boundary': record,
                                               'configuration_scope_hash': status['configuration_scope_hash']})
    backup = bridge.settings.state/'before-native-curator.json'
    if not backup.exists():
        atomic_json(backup, {'enabled': status['enabled'], 'consolidate': status['consolidate'],
                            'enterprise_boundary': approved})
    result = bridge.api.configure_curator()
    after = bridge.api.read(kind='curator_status')
    if after['configuration_scope_hash'] != status['configuration_scope_hash']:
        raise RuntimeError('Native Curator configuration changed unrelated settings; enterprise binding was not updated')
    if approved:
        # The same operator-authorized deployment changed only Curator flags.
        # Refresh its config digest without changing identity or learning scope.
        atomic_json(Path(policy['enterprise_boundary']), make_boundary(bridge.settings))
    store.set('native-curator-status', after)
    store.set('native-curator-config-result', {'state': 'applied', **result})
    store.set('native-curator-config-request', None)
