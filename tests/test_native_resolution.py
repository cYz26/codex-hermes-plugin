"""Exercise shipped native patch code with an isolated journal and no model."""
import contextlib
import hashlib
import json
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def patched_module(path):
    root = Path(__file__).resolve().parents[1]
    data = (root / 'patches/v0.23.0/native-learning-api-v1.patch').read_text()
    section = re.search(r'(?ms)^diff --git a/' + re.escape(path) + r'.*?(?=^diff --git|\Z)', data).group()
    source = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+') and not line.startswith('+++'))
    module = types.ModuleType(path.removesuffix('.py').replace('/', '.'))
    exec(compile(source, path, 'exec'), module.__dict__)
    return module


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class NativeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outcome = {'run_status': 'unknown', 'applied': [{'tool': 'skill_manage', 'name': 'fixture'}],
                        'staged': [], 'refused': [], 'uncertain': []}
        self.results = patched_module('agent/learning_results.py')
        self.modules = patch.dict('sys.modules', {
            'agent.learning_results': self.results,
            'agent.learning_artifacts': types.SimpleNamespace(fingerprint=fingerprint,
                skill=lambda home, name: {'version': 'v1'}),
            'agent.learning_runtime': types.SimpleNamespace(NativeLearningRuntime=lambda: types.SimpleNamespace(
                review=lambda *a: self.outcome, close=lambda: {})),
            'hermes_cli.plugins_loader': types.SimpleNamespace(_plugin_home_scope=lambda home: contextlib.nullcontext())})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.api = patched_module('agent/learning_service.py').LearningService(Path(self.tmp.name), 'fixture')
        self.addCleanup(self.api.close)
        self.handle = self.api.open({'host': 'fixture', 'project': '/fixture', 'chat': 'actual'})['handle']
        self.messages = [{'role': 'user', 'content': 'selected material'}]
        self.intent = self.api.request_review(self.handle, request_id='first', messages=self.messages, memory=False)
        self.api.run_review(self.handle, self.intent['id'], attempt_id='attempt')
        self.inspection = {'attempt_id': 'attempt', 'outcome_hash': fingerprint(self.outcome),
            'artifact_versions': {'fixture': 'v1'}, 'note': 'Inspected original receipts, ledger and all current files.',
            'evidence': 'local-audit:inspection-sha256'}

    def resolve(self, **overrides):
        return self.api.close_review_without_retry(self.handle, self.intent['id'], **{**self.inspection, **overrides})

    def test_inspection_unblocks_new_work_without_rewriting_or_replaying_the_old_attempt(self):
        with self.assertRaisesRegex(ValueError, 'uncertain review'):
            self.api.request_review(self.handle, request_id='second', messages=self.messages)
        result = self.resolve()
        self.assertEqual(result['state'], 'closed_without_retry')
        self.assertEqual(self.resolve(), result)
        old = self.api.read(kind='request', name='first')
        self.assertEqual(old['attempt']['run_status'], 'unknown')
        self.assertEqual(old['attempt']['applied'], self.outcome['applied'])
        self.assertEqual(self.api.run_review(self.handle, self.intent['id'], attempt_id='attempt')['run_status'], 'unknown')
        self.api.request_review(self.handle, request_id='second', messages=self.messages)
        with self.assertRaises(ValueError):
            self.resolve(note='A different inspection cannot replace the first')

    def test_inspection_requires_exact_outcome_and_complete_current_artifact_versions(self):
        for changed in ({'outcome_hash': 'stale'}, {'artifact_versions': {}},
                        {'artifact_versions': {'fixture': 'v0'}}, {'attempt_id': 'wrong'}, {'note': ''}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.resolve(**changed)
        with self.assertRaises(ValueError):
            self.api.close_review_without_retry('foreign', self.intent['id'], **self.inspection)

    def test_unconfirmed_or_non_skill_operations_cannot_be_closed_by_artifact_inspection(self):
        for changed in ({'uncertain': ['missing result']}, {'staged': [{'tool': 'memory'}]},
                        {'refused': [{'tool': 'skill_manage'}]}, {'applied': [{'tool': 'memory'}]}):
            outcome = {**self.outcome, **changed}
            with self.api.db:
                self.api.db.execute('UPDATE attempts SET payload=? WHERE id=?', (json.dumps(outcome), 'attempt'))
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.resolve(outcome_hash=fingerprint(outcome))

    def test_native_exit_reason_is_preserved_without_reclassifying_unknown_as_success(self):
        state = types.SimpleNamespace(review_messages=[], review_usage={},
            result={'completed': False, 'turn_exit_reason': 'max_iterations_reached(16/16)'})
        outcome = self.results.review_outcome(state)
        self.assertEqual(outcome['run_status'], 'unknown')
        self.assertEqual(outcome['turn_exit_reason'], 'max_iterations_reached(16/16)')
