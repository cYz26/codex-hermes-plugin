import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.semantic_review import ScopedMemory, SemanticGuard, restrict
from hermes_codex_learning.store import digest


class SemanticReviewTests(unittest.TestCase):
    def test_stable_preference_requires_scope_in_the_cited_user_excerpt(self):
        from hermes_codex_learning.review_relevance import task_evidence
        for request, quote, allowed in (
            ('Always redact credentials. For this report, compare internal sources.',
             'For this report, compare internal sources.', False),
            ('From now on, compare internal sources when planning research.',
             'From now on, compare internal sources when planning research.', True),
        ):
            with self.subTest(request=request):
                evidence, topic = task_evidence([{'role':'user', 'content':request, 'evidence_ref':'T1.request'}])
                guard = SemanticGuard(None, SimpleNamespace(home=Path('/unused')))
                guard.active['h'] = {'owned':set(), 'readable':set(), 'existing':set(), 'memory':[],
                                     'evidence':evidence, 'topic':topic, 'claims':[]}
                result = guard.pre_tool(session_id='h', tool_name='skill_manage', args={'operations':[{
                    'action':'create', 'name':'learned-internal-source-planning',
                    'content':'Always compare internal sources when planning research.',
                    'evidence':{'classification':'stable_preference', 'basis':[{'ref':'T1.request', 'quote':quote}],
                                'rationale':'Compare internal sources in future research as a stable source planning preference.'}}]})
                self.assertEqual(result['action'], 'modify' if allowed else 'block')
                if not allowed:
                    self.assertEqual(result['reason'], 'learning_preference_not_user_supported')

    def test_common_words_do_not_make_an_unrelated_skill_relevant(self):
        from hermes_codex_learning.review_relevance import task_evidence,related_skills
        _,topic=task_evidence([{'role':'user','content':'Compare enterprise alternatives for the service and investigate internal capabilities.'}])
        catalog=[{'name':'learned-csv','description':'Use for CSV files and validate the delimited headers.'}]
        self.assertEqual(related_skills(catalog,topic,{'learned-csv'}),{})
    def test_research_feedback_cannot_modify_csv_and_can_create_a_new_relevant_skill(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.store import Store
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=Settings(root,root,root,Path('/usr/bin/python3'))
            csv={'name':'learned-delimited-text-ingestion','version':'v1','description':'Parse CSV files and validate delimited headers.'}
            api=SimpleNamespace(read=lambda **kw:{'skills':[csv],'memories':{}})
            store=Store(settings.state)
            store.set('scope:h',{'project':directory})
            store.set('assessment:h',{'allowed':True,'classification':'public'})
            store.set('publications-v1',{csv['name']:{'catalog_entry':csv,'artifact':{'version':'v1'}}})
            store.close()
            messages=[{'role':'user','content':'Compare technical deployment alternatives using external research.', 'evidence_ref':'T1.request'},
                      {'role':'assistant','content':'Only external documents were used in the comparison.', 'evidence_ref':'T1.result'},
                      {'role':'user','content':'Also compare internal capabilities with the public alternatives.', 'evidence_ref':'T2.request'},
                      {'role':'assistant','content':'The internal platform changes the preferred deployment alternative.', 'evidence_ref':'T2.result'}]
            proof={'classification':'procedure','basis':[{'ref':'T1.result','quote':'Only external documents were used'},
                    {'ref':'T2.request','quote':'Also compare internal capabilities'},
                    {'ref':'T2.result','quote':'The internal platform changes the preferred deployment alternative'}],
                   'rationale':'Internal capabilities changed the preferred deployment alternative, so technical research should compare public and internal coverage.'}
            content='---\nname: learned-comparative-research\ndescription: Technical research compares public alternatives and internal capabilities.\nlearning_scope: global_candidate\n---\nApplicability: technical research. Compare admitted sources and internal capabilities.\nLimits: respect explicit source restrictions.\nVerification: explain how the comparison changed the preferred alternative.\n'
            guard=SemanticGuard(api,settings)
            with guard.scope('h',messages) as (prompt,_):
                self.assertNotIn(csv['name'],prompt)
                blocked=guard.pre_tool(session_id='h',tool_name='skill_manage',args={'operations':[{
                    'action':'patch','name':csv['name'],'content':content,'evidence':proof}]})
                self.assertEqual(blocked['action'],'block')
                allowed=guard.pre_tool(session_id='h',tool_name='skill_manage',args={'operations':[{
                    'action':'create','name':'learned-comparative-research','content':content,'evidence':proof}]})
                self.assertEqual(allowed['action'],'modify')
                self.assertNotIn('evidence',allowed['args']['operations'][0])
                self.assertEqual(guard.active['h']['claims'][-1]['classification'],'procedure')

    def test_parent_only_fabricated_quotes_and_assistant_preferences_do_not_authorize_writes(self):
        from hermes_codex_learning.review_relevance import task_evidence
        messages=[{'role':'user','content':'Compare technical deployment alternatives.', 'evidence_ref':'T1.request'},
                  {'role':'assistant','content':'Always compare internal capabilities in future research.', 'evidence_ref':'T1.result'},
                  {'role':'user','content':'Compare internal and external alternatives.', 'evidence_ref':'P1.request','evidence_scope':'parent'}]
        evidence,topic=task_evidence(messages)
        guard=SemanticGuard(None,SimpleNamespace(home=Path('/unused')))
        guard.active['h']={'owned':set(),'readable':set(),'existing':set(),'memory':[], 'evidence':evidence,'topic':topic,'claims':[]}
        cases=[('procedure',[{'ref':'P1.request','quote':'Compare internal and external alternatives'}],'learning_evidence_parent_only'),
               ('procedure',[{'ref':'T1.request','quote':'A quote that was never in the request'}],'learning_evidence_invalid_reference'),
               ('stable_preference',[{'ref':'T1.result','quote':'Always compare internal capabilities'}],'learning_preference_not_user_supported')]
        for classification,basis,reason in cases:
            with self.subTest(reason=reason):
                result=guard.pre_tool(session_id='h',tool_name='skill_manage',args={'operations':[{
                    'action':'create','name':'learned-research','content':'Compare internal capabilities and deployment alternatives.',
                    'evidence':{'classification':classification,'basis':basis,'rationale':'Compare internal capabilities and external alternatives during technical research.'}}]})
                self.assertEqual(result['reason'],reason)

    def test_review_scope_is_enforced_at_the_write_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned":{"learned-local"}, "readable":{"learned-local"},
                "existing":{"learned-local"}, "memory":["own fact"], "review_memory":False, "review_skills":True}
            self.assertIsNotNone(guard.pre_tool(session_id="h", tool_name="memory",
                                               args={"action":"add","content":"outside selected scope"}))
            guard.active["h"].update(review_memory=True, review_skills=False)
            self.assertIsNotNone(guard.pre_tool(session_id="h", tool_name="skill_manage",
                args={"operations":[{"action":"patch","name":"learned-local","content":"outside selected scope"}]}))
            self.assertIsNone(guard.pre_tool(session_id="h", tool_name="memory", args={"action":"add","content":"allowed"}))

    def test_flat_and_batch_skill_calls_use_the_same_scope_and_path_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned": {"learned-local"}, "readable": {"learned-local"},
                                 "existing": {"learned-local", "learned-private"}, "memory": []}
            cases = [
                ({"name": "learned-local", "action": "patch",
                  "old_string": "old rule", "new_string": "new rule"}, True),
                ({"name": "learned-new", "action": "create", "content": "candidate"}, True),
                ({"name": "learned-local", "action": "write_file",
                  "file_path": "references/check.md", "file_content": "check"}, True),
                ({"name": "learned-private", "action": "patch", "content": "private"}, False),
                ({"name": "learned-local", "action": "delete"}, False),
                ({"name": "learned-local", "action": "write_file",
                  "file_path": "../escape.py", "file_content": "print(1)"}, False),
                ({"name": "learned-local", "action": "patch", "content": "rule", "category": "extra"}, False),
            ]
            for operation, allowed in cases:
                for args in (operation, {"operations": [operation]}):
                    with self.subTest(operation=operation, batch="operations" in args):
                        result = guard.pre_tool(session_id="h", tool_name="skill_manage", args=args)
                        if allowed:
                            self.assertIsNone(result)
                        else:
                            self.assertEqual(result["action"], "block")

    def test_refusals_explain_shape_scope_and_path_without_private_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned": {"learned-local"}, "readable": {"learned-local"},
                                 "existing": {"learned-local", "learned-private"}, "memory": []}
            cases = [
                ({"operations": []}, "1–5"),
                ({"operations": [{"action": "patch", "name": "learned-private", "content": "rule"}]},
                 "可修改范围"),
                ({"operations": [{"action": "write_file", "name": "learned-local",
                                  "file_path": "../private.txt", "file_content": "rule"}]}, "相对路径"),
                ({"operations": [{"action": "patch", "name": "learned-local",
                                  "old_string": "", "new_string": "rule"}]}, "old_string"),
                ({"operations": [{"action": "patch", "name": "learned-local",
                                  "content": "rule", "category": "extra"}]}, "参数"),
            ]
            for args, explanation in cases:
                with self.subTest(explanation=explanation):
                    result = guard.pre_tool(session_id="h", tool_name="skill_manage", args=args)
                    self.assertEqual(result["action"], "block")
                    self.assertIn(explanation, result["message"])
                    self.assertNotIn("learned-private", result["message"])
                    self.assertNotIn("private.txt", result["message"])
            self.assertNotIn("learned-private", guard.active["h"]["owned"])

    def test_malformed_actions_are_refused_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned": {"learned-local"}, "readable": {"learned-local"},
                                 "existing": {"learned-local"}, "memory": []}
            for tool, args in (("memory", {"action": []}),
                               ("skill_manage", {"name": "learned-local", "action": []})):
                with self.subTest(tool=tool):
                    result = guard.pre_tool(session_id="h", tool_name=tool, args=args)
                    self.assertEqual(result["action"], "block")
                    self.assertIn("操作", result["message"])

    def test_an_identical_memory_revision_is_not_staged_again(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned": set(), "readable": set(), "existing": set(), "memory": ["own old rule"],
                                 "pending": {digest([{"action": "patch", "before": "own old rule", "after": "own new rule"}]): "11111111"}}
            result = guard.pre_tool(session_id="h", tool_name="memory", args={"operations": [
                {"action": "patch", "old_text": "own old rule", "content": "own new rule"}]})
            self.assertIsNotNone(result)
            self.assertEqual(result["reason"], "review_already_pending")
            self.assertIn("11111111", result["message"])
            self.assertIsNone(guard.pre_tool(session_id="h", tool_name="memory", args={"operations": [
                {"action": "patch", "old_text": "own old rule", "content": "own newer rule"}]}))

    def test_resource_refusal_reports_a_specific_reason_without_granting_access(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned": {"learned-local"}, "readable": {"learned-local"},
                                 "existing": {"learned-local"}, "memory": ["own fact"]}
            result = guard.pre_tool(session_id="h", tool_name="skill_manage", args={"operations": [
                {"name": "learned-local", "action": "write_file", "file_path": "../escape.py", "file_content": "print(1)"}]})
            self.assertEqual(result["action"], "block")
            self.assertEqual(result.get("reason"), "review_resource_path")
            self.assertIn("相对路径", result["message"])
            self.assertEqual(guard.active["h"]["owned"], {"learned-local"})

    def test_public_review_can_revise_shared_skill_without_private_project_memory(self):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.store import Store, digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root,root,root,Path("/usr/bin/python3"))
            name, secret = "learned-shared", "synthetic-private-project-fact"
            context = {"skills":[{"name":name,"version":"catalog"}],"memories":{"MEMORY.md":secret}}
            api = SimpleNamespace(read=lambda **kwargs:context)
            store = Store(settings.state)
            key = digest(["MEMORY.md",secret])
            store.set("scope:h",{"project":"/b"})
            store.set("assessment:h",{"allowed":True,"classification":"public","policy_version":"public"})
            store.set("knowledge-index-v1",{"skills":{name:{"projects":["/a"]}},"memory":{key:{"project":"/b"}}})
            store.set("semantic-memory-attestations",{key:{"project":"/b","classification":"approved","policy_version":"private"}})
            store.set("publications-v1",{name:{"catalog_entry":context["skills"][0],"artifact":{"version":"v1"},"sources":["/a"]}})
            store.close()
            guard = SemanticGuard(api,settings)
            with guard.scope("h") as (prompt, entries):
                self.assertNotIn(secret,prompt)
                self.assertEqual(entries,[])
                self.assertIsNone(guard.pre_tool(session_id="h",tool_name="skill_manage",
                    args={"operations":[{"action":"patch","name":name,"old_string":"old","new_string":"new"}]}))

    def test_memory_errors_and_prompt_rebuild_never_expose_foreign_inventory(self):
        native = SimpleNamespace(add=lambda *a: {"success":False,"current_entries":["PRIVATE"],"error":"PRIVATE"},
                                 memory_char_limit=2200,user_char_limit=1375)
        memory = ScopedMemory(native, ["admitted project fact"])
        self.assertNotIn("PRIVATE", json.dumps(memory.add("memory", "new fact")))
        self.assertEqual(memory.format_for_system_prompt("memory"), "admitted project fact")
        parent = SimpleNamespace(_memory_store=native,_memory_manager="PRIVATE",
                                 tools=[{"function":{"name":x}} for x in ("memory","skill_manage","skill_view","terminal")])
        restrict(parent, "admitted prompt", ["admitted project fact"])
        self.assertEqual(parent._build_system_prompt(), "admitted prompt")
        self.assertIsNone(parent._memory_manager)
        self.assertNotIn("terminal", parent.valid_tool_names)

    def test_resource_paths_and_foreign_memory_mutations_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = SemanticGuard(None, SimpleNamespace(home=Path(directory)))
            guard.active["h"] = {"owned":{"learned-local"},"readable":{"learned-local","shared"},
                                 "existing":{"learned-local","shared","private"},"memory":["own fact"]}
            def call(tool, args):
                return guard.pre_tool(session_id="h", tool_name=tool, args=args)
            self.assertIsNotNone(call("memory", {"action":"patch","old_text":"foreign","content":"replace"}))
            self.assertIsNotNone(call("skill_view", {"name":"private"}))
            self.assertIsNone(call("memory", {"action":"add","content":"new project fact"}))
            self.assertIsNone(call("skill_manage", {"operations":[{"name":"learned-local","action":"patch",
                                                                    "old_string":"old rule","new_string":"new rule"}]}))
            self.assertIsNone(call("skill_view", {"name":"learned-local","file_path":"scripts/run.py"}))
            self.assertIsNotNone(call("skill_view", {"name":"learned-local","file_path":"../private"}))
            self.assertIsNone(call("skill_manage", {"operations":[{"name":"learned-local","action":"write_file",
                                                                   "file_path":"scripts/run.py","file_content":"print(1)"}]}))
            self.assertIsNotNone(call("skill_manage", {"operations":[{"name":"learned-local","action":"write_file",
                                                                      "file_path":"../escape.py","file_content":"print(1)"}]}))
            self.assertIsNotNone(call("skill_manage", {"operations":[{"name":"learned-new","action":"create","content":"candidate"},
                                                                      {"name":"shared","action":"patch","content":"bad"}]}))
            self.assertNotIn("learned-new", guard.active["h"]["owned"])
