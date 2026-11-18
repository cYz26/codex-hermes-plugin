import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.store import atomic_json, digest
from hermes_codex_learning.supervisor import Supervisor
from hermes_codex_learning.systemd import systemd
import test_updates as fixtures


class CloudLifecycleTests(unittest.TestCase):
    def test_site_publication_failure_does_not_leave_committed_worker_paused(self):
        fixture = fixtures.ActivationTests()
        fixture.setUp()
        try:
            fixture.before['updates'] = {'site_manifest': str(fixture.root / 'site/migration.json')}
            atomic_json(fixture.config, fixture.before)
            fixture.candidate['site'] = {'fixture': True}
            atomic_json(fixture.updater.directory / 'candidate.json', fixture.candidate)
            worker = fixtures.FakeWorker(fixture.home)
            supervisor = Supervisor(fixture.config, worker=worker)
            with patch('hermes_codex_learning.site_updates.publish_binding', side_effect=OSError('fixture failure')):
                supervisor.activate()
            self.assertTrue(worker.resumed)
            transaction = json.loads((fixture.updater.directory / 'transaction.json').read_text())
            self.assertEqual(transaction['state'], 'committed')
            self.assertEqual(transaction['site_publication'], 'pending')
            with patch('hermes_codex_learning.site_updates.publish_binding'):
                supervisor.finish_site_publication()
            transaction = json.loads((fixture.updater.directory / 'transaction.json').read_text())
            self.assertEqual(transaction['site_publication'], 'complete')
        finally:
            fixture.tearDown()

    def test_active_service_replacement_drains_restarts_and_checks_new_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'bridge.json'
            atomic_json(config, dict(home=str(root/'learning'), project=str(root),
                                    source=str(root/'native'), python=sys.executable))
            unit = root / '.config/systemd/user/hermes-learning-pilot.service'
            unit.parent.mkdir(parents=True)
            unit.write_text('[Service]\nExecStart=/old/service\n')
            commands = []
            def run(argv, **kwargs):
                commands.append(argv)
                return SimpleNamespace(returncode=0, stdout='active\n')
            with patch('hermes_codex_learning.systemd.Path.home', return_value=root), \
                 patch('hermes_codex_learning.systemd.subprocess.run', side_effect=run), \
                 patch('hermes_codex_learning.systemd.drain_service', create=True) as drain, \
                 patch('hermes_codex_learning.systemd.verify_service', create=True) as verify, \
                 contextlib.redirect_stdout(io.StringIO()):
                systemd(SimpleNamespace(settings=str(config), start=True, replace=True, dry_run=False))
            drain.assert_called_once()
            verify.assert_called_once()
            self.assertIn(['systemctl', '--user', 'restart', unit.name], commands)

    def test_workflow_delivery_and_session_close_keep_the_actual_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = Settings(root/'home', root, root, Path(sys.executable), capture_scope='global',
                                data_policy={'enabled': True, 'workflow_projection': True})
            class API:
                def __init__(self):
                    self.sources, self.observed, self.deliveries = {}, [], []
                def open(self, source):
                    handle = digest(source)
                    self.sources[handle] = source
                    return {'handle': handle}
                def observe(self, handle, value, **kwargs):
                    self.observed.append(handle)
                def record_usage(self, token, evidence):
                    self.deliveries.append((token, evidence))
                    return {'recorded': True}
            api = API()
            bridge = Bridge(settings, api)
            events = [
                dict(hook_event_name='UserPromptSubmit', prompt='PRIVATE-TASK'),
                dict(hook_event_name='PostToolUse', tool_use_id='load', tool_name='mcp__hermes_learning__skill_load',
                     tool_response={'structuredContent': {'name':'workflow-example','delivery_token':'fixture-token',
                                                          'content':'PRIVATE-SKILL'}}),
                dict(hook_event_name='Stop', last_assistant_message='PRIVATE-ANSWER'),
                dict(hook_event_name='SessionEnd'),
            ]
            try:
                for event in events:
                    hook(dict(event, session_id='chat', turn_id='turn', cwd=directory), settings)
                bridge.ingest()
                self.assertEqual(len(api.deliveries), 1)
                self.assertEqual(set(bridge.store.get('closing').values()), set(api.observed))
                payloads = ''.join(row[0] for row in bridge.store.db.execute('SELECT payload FROM events'))
                self.assertNotIn('PRIVATE-', payloads)
                source = bridge.store.get('workflow-local-source:' + api.observed[0])
                self.assertEqual(source['project'], str(root))
            finally:
                bridge.store.close()

    def test_fetch_release_redirect_fallback_uses_configured_proxy_and_respects_no_proxy(self):
        import os
        import urllib.error
        from hermes_codex_learning.updates import _url_opener, fetch_release

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = Settings(root / 'home', root, root, Path(sys.executable),
                                network_environment={'HTTPS_PROXY': 'http://proxy.internal:8118',
                                                     'NO_PROXY': 'localhost,127.0.0.1,.internal'})
            with patch.dict(os.environ, {}, clear=True):
                import urllib.request
                github_opener = _url_opener(settings, 'https://github.com/NousResearch/hermes-agent/releases/latest')
                bypass_opener = _url_opener(settings, 'https://service.internal/v1')
                github_proxies = next(getattr(h, 'proxies', {}) for h in github_opener.handlers if isinstance(h, urllib.request.ProxyHandler))
                self.assertEqual(github_proxies.get('https'), 'http://proxy.internal:8118')
                self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in bypass_opener.handlers))

                class FakeResponse:
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def geturl(self): return 'https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1'

                seen_proxies = []
                class FakeOpener:
                    def __init__(self, proxies): self.proxies = proxies
                    def open(self, req, timeout=20):
                        seen_proxies.append((req.full_url, dict(self.proxies)))
                        if 'api.github.com' in req.full_url:
                            raise urllib.error.HTTPError(req.full_url, 403, 'rate limit exceeded', {}, None)
                        return FakeResponse()

                with patch('hermes_codex_learning.updates.urllib.request.build_opener',
                           side_effect=lambda handler: FakeOpener(handler.proxies)):
                    release = fetch_release(settings)
                self.assertEqual(release['tag'], 'v0.23.1')
                self.assertTrue(release['api_rate_limited'])
                self.assertEqual(len(seen_proxies), 2)
                self.assertEqual(seen_proxies[1][1].get('https'), 'http://proxy.internal:8118')

    def test_ls_remote_tag_commit_retries_transient_proxy_disconnects_over_http11(self):
        import subprocess
        from hermes_codex_learning.updates import ls_remote_tag_commit

        calls = []
        commit = '3b421578588243f920886e6fe79a6faa87ea026e'
        def fake_run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(128, argv, stderr='fatal: expected flush after ref listing')
            return SimpleNamespace(returncode=0, stdout=f"{'a'*40}\trefs/tags/v0.23.1\n{commit}\trefs/tags/v0.23.1^{{}}\n")

        with patch('hermes_codex_learning.updates.subprocess.run', side_effect=fake_run), \
             patch('hermes_codex_learning.updates.time.sleep'):
            resolved = ls_remote_tag_commit('https://github.com/NousResearch/hermes-agent.git', 'v0.23.1')
        self.assertEqual(resolved, commit)
        self.assertEqual(len(calls), 2)
        self.assertIn('http.version=HTTP/1.1', calls[0])
        self.assertIn('--tags', calls[0])

    def test_network_config_previews_persists_with_backup_and_rejects_embedded_credentials(self):
        import os
        import subprocess
        from hermes_codex_learning.operations import execute

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / 'bridge.json'
            atomic_json(config, {'home': str(root / 'home'), 'project': str(root),
                                 'source': str(root / 'src'), 'python': sys.executable})
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://user:secret@proxy.internal:8118'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'cannot embed credentials'):
                    execute(SimpleNamespace(command='network-config', settings=str(config), clear=False, apply=True))
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.internal:8118',
                                         'NO_PROXY': 'localhost,127.0.0.1'}, clear=True):
                preview = execute(SimpleNamespace(command='network-config', settings=str(config), clear=False, apply=False))
                self.assertFalse(preview['applied'])
                self.assertNotIn('network_environment', json.loads(config.read_text()))
                cli_out = subprocess.check_output(
                    [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/bridge'),
                     'network-config', '--settings', str(config)],
                    env={**os.environ, 'PATH': '/usr/bin:/bin'}, text=True,
                )
                self.assertFalse(json.loads(cli_out)['applied'])
                applied = execute(SimpleNamespace(command='network-config', settings=str(config), clear=False, apply=True))
                self.assertTrue(applied['applied'])
                saved = json.loads(config.read_text())
                self.assertEqual(saved['network_environment']['HTTPS_PROXY'], 'http://proxy.internal:8118')
                self.assertEqual(saved['network_environment']['NO_PROXY'], 'localhost,127.0.0.1')
                self.assertTrue(config.with_name('bridge.before-network-config.json').is_file())
                cleared = execute(SimpleNamespace(command='network-config', settings=str(config), clear=True, apply=True))
                self.assertEqual(cleared['network_environment'], {})
                self.assertEqual(json.loads(config.read_text())['network_environment'], {})

    def test_release_scoped_site_manifest_resolves_while_older_current_site_remains_active(self):
        from hermes_codex_learning.site_updates import has_site_adapter, resolve_site_manifest
        from hermes_codex_learning.updates import Updater, file_hash

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            site_dir = root / 'site-migration'
            site_dir.mkdir()
            v0230_patch = site_dir / 'site.patch'
            v0230_patch.write_text('diff --git a/a.py b/a.py\n')
            v0230_commit = '2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19'
            v0231_commit = '3b421578588243f920886e6fe79a6faa87ea026e'
            v0230_record = {'state': 'verified', 'source_tree': 'tree-230', 'base_commit': v0230_commit,
                            'patch_sha256': file_hash(v0230_patch), 'tests': {'tests/a.py': '0' * 64}}
            atomic_json(site_dir / 'migration.json', v0230_record)
            atomic_json(site_dir / 'current-site.json', v0230_record)

            recipe_231 = {'tag': 'v0.23.1', 'base_commit': v0231_commit,
                          'mechanism_review': {'classification': 'adapted', 'reason': 'reviewed', 'agreed_plan': 'docs/SHARED-LEARNING.zh-CN.md'}}
            self.assertFalse(has_site_adapter(site_dir / 'migration.json', recipe_231))

            settings = Settings(root / 'home', root, root / 'src', Path(sys.executable),
                                official_source=root / 'official', upgrade_official=True,
                                site_manifest=site_dir / 'migration.json')
            updater = Updater(settings)
            with patch.object(updater, 'current', return_value='v0.23.0'), \
                 patch.object(updater, 'recipes', return_value={'v0.23.0': {'base_commit': v0230_commit}, 'v0.23.1': recipe_231}), \
                 patch('hermes_codex_learning.updates.git', return_value=v0230_commit):
                status_before = updater.check(fetch=lambda _s: {'tag': 'v0.23.1'})
                self.assertEqual(status_before['state'], 'adaptation_required')
                self.assertTrue(status_before['assessment']['site_only'])

                rel_dir = site_dir / 'releases' / 'v0.23.1'
                rel_dir.mkdir(parents=True)
                v0231_patch = rel_dir / 'site.patch'
                v0231_patch.write_text('diff --git a/b.py b/b.py\n')
                atomic_json(rel_dir / 'migration.json', {
                    'state': 'prepared', 'source_tree': 'tree-231', 'base_commit': v0231_commit,
                    'patch_sha256': file_hash(v0231_patch), 'tests': {'tests/b.py': '1' * 64},
                })
                manifest, patch_path, sha = resolve_site_manifest(site_dir / 'migration.json', recipe_231)
                self.assertEqual(manifest['base_commit'], v0231_commit)
                self.assertEqual(patch_path, v0231_patch)
                self.assertEqual(sha, file_hash(v0231_patch))
                status_after = updater.check(fetch=lambda _s: {'tag': 'v0.23.1'})
                self.assertEqual(status_after['state'], 'waiting_for_official')
                self.assertEqual(status_after['target'], 'v0.23.1')

    def test_guarded_native_update_defers_gateway_restart_and_recovers_from_github_rate_limit(self):
        import types
        from hermes_codex_learning.official_runtime import guarded_native_update

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            before = '2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19'
            commit = '3b421578588243f920886e6fe79a6faa87ea026e'
            (source / 'pyproject.toml').write_text('[project]\nname = "hermes-agent"\nversion = "0.23.0"\n')
            seen_argv = []

            class FakeTarget:
                def __init__(self, channel, resolved_channel, repository, commit=None, version=None, ahead=False):
                    self.channel = resolved_channel
                    self.repository = repository
                    self.commit = commit
                    self.version = version
                    self.ahead = ahead

            fake_sr = types.ModuleType('hermes_cli.source_releases')
            fake_sr.OFFICIAL_REPOSITORY = 'NousResearch/hermes-agent'
            fake_sr.SourceTarget = FakeTarget
            # Simulate shallow checkout + rate-limited _github_compare returning HEAD (`before`).
            fake_sr._head_containing = lambda *_a, **_kw: before
            def rate_limited_resolve(*_a, **_kw):
                raise ValueError('No published stable release could be verified: GitHub API rate limit exceeded')
            fake_sr.resolve_source_target = rate_limited_resolve

            fake_main = types.ModuleType('hermes_cli.main')
            fake_main.PROJECT_ROOT = source
            def run_main():
                seen_argv.append(list(sys.argv))
                selected = fake_sr.resolve_source_target('stable', ['git'], source, forward_only=True)
                self.assertEqual(selected.commit, commit)
                self.assertEqual(selected.version, '0.23.1')
                return 0
            fake_main.main = run_main
            fake_pkg = types.ModuleType('hermes_cli')
            fake_pkg.source_releases = fake_sr
            fake_pkg.main = fake_main
            fake_bootstrap = types.ModuleType('hermes_bootstrap')

            def fake_git(_src, *args):
                return before if args[:2] == ('rev-parse', 'HEAD') else ''

            with patch.dict(sys.modules, {'hermes_cli': fake_pkg, 'hermes_cli.source_releases': fake_sr,
                                         'hermes_cli.main': fake_main, 'hermes_bootstrap': fake_bootstrap}), \
                 patch('hermes_codex_learning.updates.git', side_effect=fake_git), \
                 patch('hermes_codex_learning.updates.fetch_release', return_value={'tag': 'v0.23.1'}), \
                 patch('hermes_codex_learning.updates.ls_remote_tag_commit', return_value=commit):
                rc = guarded_native_update(source, before, 'v0.23.1', commit, defer_gateway_restart=True)
            self.assertEqual(rc, 0)
            self.assertIn('--no-gateway-restart', seen_argv[0])

    def test_supervisor_snapshot_and_restore_include_pending_and_shared_knowledge(self):
        from hermes_codex_learning.store import Store
        from hermes_codex_learning.supervisor import restore, snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / 'home'
            (home / 'pending/memory').mkdir(parents=True)
            (home / 'pending/memory/p1.json').write_text('{"id":"p1"}')
            (home / 'shared-knowledge/personal/memories').mkdir(parents=True)
            (home / 'shared-knowledge/personal/memories/USER.md').write_text('Prefer concise verification.\n')
            (home / 'learning').mkdir(parents=True)
            settings = Settings(home, root, root, Path(sys.executable))
            store = Store(settings.state)
            try:
                store.set('publication-reviews-v1', {'skill-a': {'decision': 'approve_generic'}})
                store.set('shared-enterprise-memory-boundaries', {'personal:user:abc': 'bound-1'})
            finally:
                store.close()
            backup = root / 'backup'
            snapshot(settings, backup)
            (home / 'pending/memory/p1.json').unlink()
            (home / 'shared-knowledge/personal/memories/USER.md').write_text('Changed.\n')
            store = Store(settings.state)
            try:
                store.set('publication-reviews-v1', {})
                store.set('shared-enterprise-memory-boundaries', {})
            finally:
                store.close()
            restore(settings, backup)
            self.assertEqual((home / 'pending/memory/p1.json').read_text(), '{"id":"p1"}')
            self.assertEqual((home / 'shared-knowledge/personal/memories/USER.md').read_text(), 'Prefer concise verification.\n')
            store = Store(settings.state)
            try:
                self.assertEqual(store.get('publication-reviews-v1'), {'skill-a': {'decision': 'approve_generic'}})
                self.assertEqual(store.get('shared-enterprise-memory-boundaries'), {'personal:user:abc': 'bound-1'})
            finally:
                store.close()

    def test_shared_temp_or_foreign_ancestor_git_cannot_hijack_project_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shared_tmp = root / 'shared-tmp'
            shared_tmp.mkdir()
            (shared_tmp / '.git').mkdir()
            shared_tmp.chmod(0o1777)
            try:
                one = shared_tmp / 'workspace-one'
                two = shared_tmp / 'workspace-two'
                one.mkdir()
                two.mkdir()
                settings = Settings(root / 'home', one, root, Path(sys.executable), capture_scope='global')
                self.assertEqual(settings.project_for(str(two)), str(two))
                repo = shared_tmp / 'real-repo'
                (repo / 'sub').mkdir(parents=True)
                (repo / '.git').mkdir()
                self.assertEqual(settings.project_for(str(repo / 'sub')), str(repo))
            finally:
                shared_tmp.chmod(0o700)

    def test_enterprise_boundary_refreshes_only_for_config_version_schema_bump(self):
        from hermes_codex_learning.enterprise import boundary, refresh_schema_only_boundary
        from test_enterprise_policy import enterprise_fixture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            policy, config = enterprise_fixture(root)
            original = "_config_version: 52\nprovider: custom:ark\nmodel: ark:gpt-6-1-sol\n"
            config.write_text(original)
            # Re-bind fixture hash to original text with _config_version.
            import hashlib
            receipt = Path(policy['enterprise_boundary'])
            rec = json.loads(receipt.read_text())
            rec['model_config_sha256'] = hashlib.sha256(original.encode()).hexdigest()
            atomic_json(receipt, rec)
            self.assertIsNotNone(boundary(policy))

            # Pure _config_version bump preserves the boundary.
            config.write_text("_config_version: 53\nprovider: custom:ark\nmodel: ark:gpt-6-1-sol\n")
            self.assertIsNone(boundary(policy))
            self.assertTrue(refresh_schema_only_boundary(policy, original.encode()))
            self.assertIsNotNone(boundary(policy))

            # Changing model/provider must NOT be auto-refreshed.
            bumped_bytes = config.read_bytes()
            config.write_text("_config_version: 54\nprovider: custom:ark\nmodel: other-model\n")
            self.assertFalse(refresh_schema_only_boundary(policy, bumped_bytes))
            self.assertIsNone(boundary(policy))
