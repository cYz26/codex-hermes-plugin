"""Isolated native Curator/tool/publication integration; the model decision is scripted."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    home = workspace/'home'
    home.mkdir(parents=True)
    os.environ['HERMES_HOME'] = str(home)
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root/'src'), str(args.source.resolve())]
    from hermes_cli.config import atomic_config_write
    atomic_config_write(home/'config.yaml', {'skills': {'write_approval': False},
        'curator': {'enabled': True, 'consolidate': True}, 'display': {'skin': 'default'}})
    from agent import curator
    from agent.learning_api import LearningFacade
    from tools.skill_manager_tool import skill_manage
    from tools import skill_usage
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.daemon import Bridge
    from hermes_codex_learning.lifecycle import maintenance
    from hermes_codex_learning.knowledge import context_view
    from hermes_codex_learning.skill_reads import load

    def body(name, rule, verified=True):
        return f'---\nname: {name}\ndescription: CSV ingestion workflow.\nlearning_scope: global_candidate\n---\nApplicability: CSV input.\nLimits: text tables.\n{rule}\n' + ('Verification: check header and row contracts.\n' if verified else '')
    def write(**kwargs):
        result = json.loads(skill_manage(**kwargs))
        assert result.get('success'), result
        return result
    for name, rule in [('learned-a', 'Normalize headers.'), ('learned-b', 'Reject malformed rows.'),
                       ('learned-pinned', 'Keep the explicit protected procedure.')]:
        write(action='create', name=name, content=body(name, rule))
        skill_usage.mark_agent_created(name)
    skill_usage.set_pinned('learned-pinned', True)
    pinned = (home/'skills/learned-pinned/SKILL.md').read_bytes()
    api = LearningFacade(home, 'lifecycle-fixture')
    settings = Settings(home, workspace/'a', args.source, Path(sys.executable), capture_scope='global',
                        checked_publication=True, quality_checks=True,
                        data_policy={'enabled': True, 'synthetic_roots': [str(workspace)]})
    bridge = Bridge(settings, api)
    bridge.native_capabilities = api.read(kind='capabilities')
    try:
        bridge.store.set('knowledge-index-v1', {'version': 1, 'memory': {}, 'skills': {
            'learned-a': {'projects': [str(workspace/'a')]},
            'learned-b': {'projects': [str(workspace/'b')]},
            'learned-pinned': {'projects': [str(workspace/'a')]}}})
        bridge.publish()
        assert len(bridge.store.get('publications-v1')) == 3
        handle = api.open({'host': 'fixture', 'project': str(workspace/'a'), 'chat': 'fixture'})['handle']
        with api._service.db:
            api._service.db.execute('UPDATE scopes SET activity=activity-10800 WHERE id=?', (handle,))
        curator.save_state({'last_run_at': (datetime.now(timezone.utc)-timedelta(days=8)).isoformat(), 'run_count': 0})
        calls = []
        def model(prompt):
            assert curator.CURATOR_REVIEW_PROMPT in prompt
            calls.append('native-consolidation')
            from tools.skill_provenance import set_current_write_origin, reset_current_write_origin
            from tools.skill_manager_guards import _reset_background_review_read_marks
            from tools.skills_tool import skill_view
            token = set_current_write_origin('background_review')
            _reset_background_review_read_marks()
            try:
                for name in ('learned-a', 'learned-b'):
                    skill_view(name)
                write(action='create', name='learned-combined',
                      content=body('learned-combined', 'Normalize headers, then reject malformed rows.', False).replace('learning_scope: global_candidate\n', ''))
                write(action='create', name='learned-generated',
                      content=body('learned-generated', 'Use one input contract across normalization and parsing.').replace('learning_scope: global_candidate\n', ''))
                write(action='write_file', name='learned-combined', file_path='references/contracts.md',
                      file_content='Keep header normalization and row shape constraints together.')
                for name in ('learned-a', 'learned-b'):
                    write(action='delete', name=name, absorbed_into='learned-combined')
            finally:
                reset_current_write_origin(token)
            return {'summary': 'Fixture consolidation.', 'final': '', 'tool_calls': []}
        original = curator._run_llm_review
        curator._run_llm_review = model
        try:
            result = maintenance(bridge)
        finally:
            curator._run_llm_review = original
        assert result['curator'] and calls == ['native-consolidation']
        assert (home/'skills/learned-pinned/SKILL.md').read_bytes() == pinned
        bridge.last_publish = 0
        bridge.publish()
        assert 'learned-combined' not in bridge.store.get('publications-v1')
        assert bridge.store.get('publication-decisions')['learned-combined']['reason'] == 'missing_applicability_limits_or_verification'
        assert 'learned-generated' in bridge.store.get('publications-v1')
        assert 'Normalize headers.' in load(settings, 'learned-a', str(workspace/'consumer'))['content']
        write(action='patch', name='learned-combined',
              content=body('learned-combined', 'Normalize headers, then reject malformed rows.'))
        bridge.last_publish = 0
        bridge.publish()
        index = bridge.store.get('knowledge-index-v1')['skills']
        assert index['learned-combined']['projects'] == [str(workspace/'a'), str(workspace/'b')]
        assert load(settings, 'learned-a', str(workspace/'consumer'))['name'] == 'learned-combined'
        assert 'row shape' in load(settings, 'learned-b', str(workspace/'consumer'), 'references/contracts.md')['content']
        view = context_view(json.loads((settings.state/'context.json').read_text()), str(workspace/'consumer'), 'CSV')
        assert 'learned-combined' in view['recommended_skills']
        restored, message = skill_usage.restore_skill('learned-a')
        assert restored, message
        bridge.last_publish = 0
        bridge.publish()
        assert bridge.store.get('knowledge-index-v1')['skills']['learned-a']['projects'] == [str(workspace/'a')]
        assert load(settings, 'learned-a', str(workspace/'consumer'))['name'] == 'learned-a'
        from agent.curator_backup import rollback
        restored, message, _ = rollback()
        assert restored, message
        bridge.last_publish = 0
        bridge.publish()
        assert bridge.store.get('knowledge-index-v1')['skills']['learned-b']['projects'] == [str(workspace/'b')]
        assert load(settings, 'learned-b', str(workspace/'consumer'))['name'] == 'learned-b'
        assert 'learned-combined' not in bridge.store.get('publications-v1')
        assert 'learned-combined' not in bridge.store.get('knowledge-index-v1')['skills']
        print(json.dumps({'passed': True, 'native_consolidations': len(calls),
            'checks': ['native_prompt', 'scheduled_completion', 'pinned_package', 'merge_origins',
                       'failed_candidate_keeps_old_publication', 'alias_and_resource_read', 'recall', 'restore_scope',
                       'native_whole_library_rollback_scope'],
            'model': 'scripted decision; native Curator and skill tools are real'}))
    finally:
        bridge.store.close()
        api.close()


if __name__ == '__main__':
    main()
