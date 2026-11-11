import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.enterprise import boundary, scope_view
from hermes_codex_learning.knowledge import context_view
from hermes_codex_learning.store import digest
from test_enterprise_policy import enterprise_fixture


class EnterpriseViewsTests(unittest.TestCase):
    def test_host_transcript_supplement_is_scrubbed_before_durable_storage(self):
        from unittest.mock import patch
        from hermes_codex_learning.capture import hook
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);policy,_=enterprise_fixture(root)
            settings=Settings(root/"home",root/"project",root,Path("/usr/bin/python3"),
                              capture_scope="global",checked_publication=True,data_policy=policy)
            observed=[]
            api=SimpleNamespace(open=lambda source:{"handle":"h"},
                                observe=lambda *args,**kwargs:observed.append(args))
            bridge=Bridge(settings,api)
            try:
                hook({"hook_event_name":"Stop","last_assistant_message":"Verified.","session_id":"chat",
                      "turn_id":"t","cwd":str(settings.project),"transcript_path":str(root/"transcript.jsonl")},settings)
                facts={"input":{"prompt":"Useful procedure\nCookie: session=synthetic-supplement-secret",
                                "source_thread":"parent","item_id":"i","item_hash":"h","method":"create_thread"},"commands":{}}
                with patch("hermes_codex_learning.host_evidence.read_turn",return_value=facts):
                    bridge.ingest()
                self.assertTrue(observed)
                stored=bridge.store.db.execute("SELECT payload FROM turns").fetchone()[0]
                self.assertIn("Useful procedure",stored)
                self.assertNotIn("synthetic-supplement-secret",stored)
                self.assertNotIn("synthetic-supplement-secret",json.dumps(observed))
            finally:
                bridge.store.close()

    def test_enterprise_observations_are_not_reclassified_by_a_later_public_turn(self):
        from hermes_codex_learning.capture import hook
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);policy,_=enterprise_fixture(root)
            settings=Settings(root/"home",root/"project",root,Path("/usr/bin/python3"),
                              capture_scope="global",checked_publication=True,data_policy=policy)
            observed=[]
            api=SimpleNamespace(open=lambda source:{"handle":"h"},
                                observe=lambda *args,**kwargs:observed.append(args))
            bridge=Bridge(settings,api)
            try:
                for turn in ("enterprise","public"):
                    if turn=="public":
                        bridge.settings=replace(settings,data_policy={"enabled":True,"synthetic_roots":[str(settings.project)]})
                    for event in ({"hook_event_name":"UserPromptSubmit","prompt":"Remember this task procedure."},
                                  {"hook_event_name":"Stop","last_assistant_message":"Verified."}):
                        hook({**event,"session_id":"chat","turn_id":turn,"cwd":str(settings.project)},bridge.settings)
                    bridge.ingest()
                    if turn=="enterprise":
                        first_count=len(observed)
                        self.assertGreater(first_count,0)
                self.assertEqual(len(observed),first_count)
                self.assertEqual(bridge.store.get("assessment:h")["audience"],"enterprise")
            finally:
                bridge.store.close()

    def test_cached_enterprise_memory_is_hidden_after_switch_to_project_mode(self):
        from hermes_codex_learning.store import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            settings=Settings(root/"home",root/"project",root,Path("/usr/bin/python3"))
            bridge=Bridge(settings,SimpleNamespace())
            try:
                entry="Internal project diagnosis."
                key=digest(["MEMORY.md",entry])
                atomic_json(settings.state/"context.json",{"memories":{"MEMORY.md":entry},"skills":[],
                    "bridge_provenance":{"memory":{key:{"project":str(settings.project)}}},
                    "enterprise_memory_boundaries":{key:"previous-enterprise"},"summary":entry})
                view=bridge.rpc("context",{"cwd":str(settings.project)})
                self.assertFalse(view["memories"])
                self.assertNotIn(entry,view["summary"])
            finally:
                bridge.store.close()

    def test_disabling_enterprise_does_not_republish_its_skills_as_public(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);policy,_=enterprise_fixture(root)
            settings=Settings(root/"home",root/"project",root,Path("/usr/bin/python3"),
                              capture_scope="global",checked_publication=True,data_policy=policy)
            artifact=settings.home/"learning/artifacts/v1";artifact.mkdir(parents=True)
            body="---\nname: learned-neutral\nlearning_scope: global_candidate\n---\nApplicability: deployments.\nLimits: verify local prerequisites.\nVerification: run checks."
            path=artifact/"SKILL.md";path.write_text(body)
            value={"name":"learned-neutral","version":"v1","resource_dir":str(artifact),
                   "content":body,"files":{"SKILL.md":hashlib.sha256(body.encode()).hexdigest()}}
            bridge=Bridge(settings,SimpleNamespace(read=lambda **kwargs:value))
            try:
                context={"version":"v1","skills":[{"name":"learned-neutral","version":"cat1"}],
                         "memories":{},"bridge_provenance":{"skills":{"learned-neutral":{"projects":[str(settings.project)]}}}}
                first=bridge.publish_checked(context)
                self.assertEqual(first["publications"]["learned-neutral"]["audience"],"enterprise")
                bridge.settings=replace(settings,data_policy={"enabled":True,"synthetic_roots":[str(settings.project)]})
                restored=bridge.publish_checked(context)
                self.assertEqual(restored["publications"]["learned-neutral"]["audience"],"enterprise")
                self.assertEqual(context_view(scope_view(restored,bridge.settings.data_policy),str(root/"other"),"deployments")["skills"],[])
            finally:
                bridge.store.close()

    def test_internal_sharing_is_labelled_and_cannot_cross_an_invalid_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);policy,config=enterprise_fixture(root);b=boundary(policy)
            settings=Settings(root/"home",root/"project",root,Path("/usr/bin/python3"),
                              capture_scope="global",checked_publication=True,data_policy=policy)
            artifact=settings.home/"learning/artifacts/v1";artifact.mkdir(parents=True)
            body="---\nname: learned-internal\nlearning_scope: global_candidate\n---\nApplicability: enterprise service deployment.\nUse https://service.internal/docs.\nLimits: approved enterprise only.\nVerification: check results."
            (artifact/"SKILL.md").write_text(body)
            value={"name":"learned-internal","version":"v1","resource_dir":str(artifact),
                   "content":body,"files":{"SKILL.md":hashlib.sha256(body.encode()).hexdigest()},
                   "file_path":"SKILL.md","content_hash":hashlib.sha256(body.encode()).hexdigest()}
            api=SimpleNamespace(read=lambda **kwargs:value)
            bridge=Bridge(settings,api)
            try:
                context={"version":"v1","skills":[{"name":"learned-internal","version":"cat1","description":"Internal deployment"}],
                         "memories":{},"bridge_provenance":{"skills":{"learned-internal":{"projects":[str(settings.project)]}}}}
                view=bridge.publish_checked(context)
                self.assertEqual(view["publications"]["learned-internal"]["audience"],"enterprise")
                bridge.store.set("knowledge-index-v1",context["bridge_provenance"])
                result=bridge.rpc("skill_load",{"name":"learned-internal","cwd":str(root/"other")})
                self.assertEqual(result["audience"],"enterprise")
                self.assertIn("service.internal",result["content"])
                global_view=context_view(scope_view(view,policy),str(root/"other"),"deployment")
                self.assertEqual(global_view["skills"][0]["audience"],"enterprise")
                config.write_text("changed model configuration")
                with self.assertRaises(PermissionError):
                    bridge.rpc("skill_load",{"name":"learned-internal","cwd":str(root/"other")})
                self.assertEqual(context_view(scope_view(view,policy),str(root/"other"),"deployment")["skills"],[])
            finally:
                bridge.store.close()

    def test_project_memory_stays_scoped_and_boundary_bound(self):
        entry="Internal reconciliation procedure."
        key=digest(["MEMORY.md",entry])
        context={"memories":{"MEMORY.md":entry},"skills":[],
                 "bridge_provenance":{"memory":{key:{"project":"/a"}}},
                 "enterprise_memory_boundaries":{key:"approved-boundary"},"active_enterprise_boundary":"approved-boundary"}
        self.assertTrue(context_view(context,"/a")["memories"])
        self.assertFalse(context_view(context,"/b")["memories"])
        self.assertFalse(context_view({**context,"active_enterprise_boundary":None},"/a")["memories"])
