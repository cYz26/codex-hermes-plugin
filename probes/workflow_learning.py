"""Opt-in real-provider probe of projected evidence and native workflow isolation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.store import atomic_json
    live = Settings.load(args.settings)
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, mode=0o700)  # Existing receipts must never be overwritten.
    home = workspace / 'home'
    home.mkdir()
    sys.path.insert(0, str(live.source))
    os.environ.update(live.network_environment)
    os.environ['HERMES_HOME'] = str(home)
    from hermes_cli.config import atomic_config_write
    from ruamel.yaml import YAML
    config = YAML(typ='safe').load((live.home / 'config.yaml').read_text())
    # Carry only non-secret routing and model settings, using the existing provider.
    model_keys = {'provider', 'default', 'api_mode', 'base_url', 'max_tokens', 'context_length'}
    provider_keys = {'base_url', 'api_mode', 'session_affinity_header', 'max_tokens', 'transport'}
    native = {'_config_version': config.get('_config_version'),
              'model': {k: v for k, v in config['model'].items() if k in model_keys},
              'providers': {name: {k: v for k, v in data.items() if k in provider_keys}
                            for name, data in config.get('providers', {}).items()},
              'agent': {k: v for k, v in config.get('agent', {}).items()
                        if k in {'reasoning_effort', 'max_tokens'}},
              'auth': {'adopt_external_logins': False}, 'gateway': {'standalone': True},
              'memory': {'memory_enabled': True, 'user_profile_enabled': True, 'write_approval': False},
              'skills': {'write_approval': False},
              'auxiliary': {'background_review': {'enabled': True, 'provider': 'auto', 'model': ''}},
              'plugins': {'isolation': 'in_process', 'enabled': ['codex-learning-bridge'], 'entries': {
                  'codex-learning-bridge': {'enabled': True, 'settings': {'learning': {'enabled': True}}}}}}
    atomic_config_write(home / 'config.yaml', native)
    plugin = home / 'plugins/codex-learning-bridge'
    plugin.parent.mkdir()
    plugin.symlink_to(ROOT / 'hermes-plugin', target_is_directory=True)
    (home / 'memories').mkdir()
    marker = 'SYNTHETIC_PRIVATE_CONTEXT_MUST_NOT_REACH_WORKFLOW_REVIEW'
    (home / 'memories/MEMORY.md').write_text(marker)
    private_skill = home / 'skills/private-fixture/SKILL.md'
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text('---\nname: private-fixture\ndescription: ' + marker + '\n---\n' + marker)
    settings = Settings(home, workspace, live.source, live.python, data_policy={'workflow_projection': True})
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from hermes_codex_learning.workflow_review import install_workflow_policy
    from hermes_codex_learning.workflow import observation
    from hermes_codex_learning.review_input import install_review_policy
    ctx = PluginContext(PluginManifest(name='codex-learning-bridge', source='user'), get_plugin_manager())
    api = ctx.learning(version=1)
    guard = install_workflow_policy(api, settings, ctx)
    install_review_policy(api)
    requests = []
    def audit(**event):
        payload = json.dumps({k: event.get(k) for k in ('request_messages', 'system_prompt')}, ensure_ascii=False)
        requests.append({'canary_present': marker in payload, 'private_catalog_present': 'private-fixture' in payload,
                         'payload_bytes': len(payload.encode()), 'model': event.get('model'),
                         'provider': event.get('provider'), 'tool_count': event.get('tool_count')})
        atomic_json(output, receipt)
    audit_registration = ctx.register_hook('pre_api_request', audit)
    receipt = {'state': 'started', 'real_provider': True, 'actual_app': False,
               'evidence': 'explicit_synthetic_workflow_fixture', 'requests': requests}
    output = workspace / 'result.json'
    atomic_json(output, receipt)
    try:
        data = {'source': {'host': 'fixture', 'project': '/private-fixture-project', 'chat': 'fixture', 'turn': '1'},
                'tools': {str(i): {'name': 'workflow_' + action, 'status': outcome}
                          for i, (action, outcome) in enumerate([('test', 'failed'), ('inspect', 'completed'),
                                                                 ('edit', 'completed'), ('test', 'completed')])},
                'status': 'completed'}
        value = observation(data)
        handle = api.open({k: value['source'][k] for k in ('host', 'project', 'chat', 'executor')})['handle']
        api.observe(handle, value, event_id='workflow-probe-observation')
        intent = api.maintenance(handle=handle, idle_flush=True)['intent']
        result = api.run_review(handle, intent, attempt_id='workflow-probe-review')
        receipt.update(state=result['run_status'], applied=result.get('applied', []), usage=result.get('usage'),
                       isolation_verified=bool(requests) and all(not r['canary_present'] and not r['private_catalog_present'] for r in requests))
        receipt['memory_unchanged'] = (home / 'memories/MEMORY.md').read_text() == marker
        receipt['skill_changed'] = any(fact.get('tool') == 'skill_manage' for fact in result.get('applied', []))
        receipt['passed'] = result['run_status'] == 'completed' and receipt['isolation_verified'] and receipt['memory_unchanged']
    except Exception as exc:
        receipt.update(state='failed', error=f'{type(exc).__name__}: {exc}', passed=False)
    finally:
        api.close()
        guard.dispose()
        audit_registration.dispose()
        atomic_json(output, receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k not in {'applied', 'usage'}}, ensure_ascii=False))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
