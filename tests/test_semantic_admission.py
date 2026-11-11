import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.policy import admit
from hermes_codex_learning.store import digest


class SemanticAdmissionTests(unittest.TestCase):
    def test_unknown_schema_and_string_synthetic_roots_do_not_admit_body(self):
        self.assertFalse(admit({"prompt":"private"},{"synthetic_roots":"/fixture"},"/project")["allowed"])
        response = {"schema_version":99,"allowed":True,"classification":"public","assessment_id":"fixture"}
        with patch('hermes_codex_learning.policy.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(response))):
            self.assertFalse(admit({"prompt":"private"},{"assessment_command":["/assessment"]},"/project")["allowed"])

    def test_replacement_cannot_turn_a_failed_execution_into_success(self):
        event = {"tool_name":"Bash","tool_response":{"exit_code":1,"output":"private output"}}
        safe = {"tool_name":"Bash","tool_response":{"exit_code":0,"output":"public"}}
        response = {"schema_version":2,"allowed":True,"classification":"public","assessment_id":"fixture",
                    "input_hash":digest(event),"output_hash":digest(safe),"payload":safe,"policy_version":"v1"}
        with patch('hermes_codex_learning.policy.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(response))):
            self.assertFalse(admit(event,{"assessment_command":["/assessment"]},"/project")["allowed"])

    def test_local_recall_keeps_only_public_skill_names_not_the_unassessed_query(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.capture import hook
        from hermes_codex_learning.store import Store, atomic_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root/"home",root,root,Path("/usr/bin/python3"),capture_scope="global",
                                checked_publication=True,data_policy={"enabled":True,"workflow_projection":True})
            skill = {"name":"workflow-verification","description":"Verify tests after edits","version":"v"}
            cache = {"version":"v","memories":{},"skills":[skill],"checked_publication":True,
                     "publications":{skill["name"]:{"catalog_entry":skill}},
                     "bridge_provenance":{"skills":{skill["name"]:{"projects":["/codex-workflow"]}}}}
            atomic_json(settings.state/"context.json",cache)
            result = hook({"hook_event_name":"UserPromptSubmit","session_id":"c","turn_id":"t","cwd":directory,
                           "prompt":"PRIVATE-QUERY 请验证测试结果"},settings)
            self.assertIn("workflow-verification",str(result))
            store = Store(settings.state)
            try:
                self.assertEqual(store.get("recall-query:c"),"workflow-verification")
                self.assertNotIn("PRIVATE-QUERY",str(store.db.execute("SELECT value FROM meta").fetchall()))
            finally:
                store.close()

    def test_mixed_turn_reaches_native_review_without_unassessed_text(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.capture import hook
        from hermes_codex_learning.daemon import Bridge
        import hashlib
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "Normalize all CSV header identifiers using NFKC before matching."
            source = root / "fixture.txt"
            source.write_text(content)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version":1,"sources":[{"path":str(source),
                "sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"classification":"synthetic","reference":"synthetic:fixture"}]}))
            settings = Settings(root/"home", root, root, Path(sys.executable),
                                data_policy={"enabled":True,"workflow_projection":True,"source_manifest":str(manifest)})
            observed = []
            api = SimpleNamespace(open=lambda source: {"handle":"scope"},
                                  observe=lambda handle, value, **kwargs: observed.append(value))
            bridge = Bridge(settings, api)
            try:
                for event in [
                    {"hook_event_name":"UserPromptSubmit","prompt":"PRIVATE request"},
                    {"hook_event_name":"PostToolUse","tool_name":"Bash","tool_use_id":"c",
                     "tool_input":{},"tool_response":{"output":content,"exit_code":0}},
                    {"hook_event_name":"Stop","last_assistant_message":"PRIVATE answer"},
                ]:
                    hook(dict(event, cwd=directory, session_id="chat", turn_id="turn"), settings)
                bridge.ingest()
                self.assertEqual(len(observed), 1)
                self.assertIn(content, json.dumps(observed))
                self.assertNotIn("PRIVATE", json.dumps(observed))
            finally:
                bridge.store.close()

    def test_v2_replacement_is_bound_to_input_and_cannot_rewrite_host_identity(self):
        event = {'hook_event_name':'UserPromptSubmit', 'session_id':'s', 'turn_id':'t',
                 'cwd':'/project', 'prompt':'private with public excerpt'}
        safe = {**event, 'prompt':'approved excerpt'}
        result = {'schema_version':2, 'allowed':True, 'classification':'public',
                  'assessment_id':'fixture', 'input_hash':digest(event), 'payload':safe,
                  'output_hash':digest(safe), 'policy_version':'fixture-v1'}
        with patch('hermes_codex_learning.policy.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(result))):
            decision = admit(event, {'assessment_command':['/assessment']}, '/project')
            self.assertEqual(decision['payload']['prompt'], 'approved excerpt')
        result['payload'] = {**safe, 'session_id':'different'}
        result['output_hash'] = digest(result['payload'])
        with patch('hermes_codex_learning.policy.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(result))):
            self.assertFalse(admit(event, {'assessment_command':['/assessment']}, '/project')['allowed'])

    def test_builtin_manifest_admits_only_hash_bound_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = 'Normalize CSV header names with NFKC before matching identifiers.'
            source = root/'fixture.txt'
            source.write_text(public)
            manifest = root/'sources.json'
            import hashlib
            manifest.write_text(json.dumps({'schema_version':1, 'sources':[
                {'path':str(source), 'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                 'classification':'synthetic', 'reference':'synthetic:normalization'}]}))
            policy = {'enabled':True, 'source_manifest':str(manifest)}
            event = {'hook_event_name':'PostToolUse', 'session_id':'s', 'turn_id':'t', 'cwd':directory,
                     'tool_name':'Bash', 'tool_use_id':'call', 'tool_response':{'output':public,'private':'PRIVATE'}, 'tool_input':{}}
            decision = admit(event, policy, directory)
            self.assertTrue(decision['allowed'])
            self.assertNotIn('PRIVATE', json.dumps(decision['payload']))
            self.assertIn(public, json.dumps(decision['payload']))
            source.write_text('changed source')
            self.assertFalse(admit(event, policy, directory)['allowed'])

    def test_v2_hash_mismatch_and_unassessed_query_stay_withheld(self):
        event = {'prompt':'PRIVATE'}
        result = {'schema_version':2, 'allowed':True, 'classification':'public', 'assessment_id':'fixture',
                  'input_hash':'wrong', 'payload':{'prompt':'safe'}, 'output_hash':'wrong','policy_version':'v1'}
        with patch('hermes_codex_learning.policy.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(result))):
            self.assertFalse(admit(event, {'assessment_command':['/assessment']}, '/p')['allowed'])
