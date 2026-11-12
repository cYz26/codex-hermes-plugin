import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class ReadonlyDeliveryTests(unittest.TestCase):
    def test_annotations_distinguish_reads_from_state_changing_operations(self):
        from hermes_codex_learning.mcp import TOOLS
        tools={t["name"]:t for t in TOOLS}
        for name in ("context","skill_load","learning_status","update_status"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        for name in ("record_application","update_check"):
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])

    def test_skill_read_has_no_persistent_writes_and_delivery_is_recorded_by_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            settings=Settings(root/"home",root,root,Path("/usr/bin/python3"),capture_scope="global")
            artifact=root/"artifact";artifact.mkdir()
            (artifact/"SKILL.md").write_text("Synthetic procedure.")
            checksum=hashlib.sha256((artifact/"SKILL.md").read_bytes()).hexdigest()
            value={"name":"fixture","version":"v1","content":"Synthetic procedure.","resource_dir":str(artifact),
                   "files":{"SKILL.md":checksum},"file_path":"SKILL.md","content_hash":checksum}
            calls=[]
            def read(**kwargs):
                calls.append(kwargs)
                return {**value,**({"delivery_token":"native-fixture"} if kwargs.get("issue_receipt",True) else {})}
            uses=[]
            api=SimpleNamespace(read=read,record_usage=lambda token,evidence: uses.append((token,evidence)) or {"recorded":True})
            bridge=Bridge(settings,api)
            try:
                bridge.cache_artifacts({"version":"catalog","skills":[{"name":"fixture","version":"v1"}]})
                bridge.store.set("knowledge-index-v1",{"skills":{"fixture":{"projects":[str(root)]}}})
                before=list(bridge.store.db.iterdump())
                count=len(calls)
                loaded=bridge.rpc("skill_load",{"name":"fixture","cwd":str(root)})
                self.assertEqual(loaded["content"],"Synthetic procedure.")
                self.assertEqual(list(bridge.store.db.iterdump()),before)
                self.assertEqual(len(calls),count)
                payload={"tool_name":"mcp__hermes_learning__skill_load","session_id":"chat","turn_id":"turn",
                         "tool_use_id":"load","bridge_project":str(root),
                         "tool_response":{"structuredContent":loaded}}
                bridge.usage(payload)
                bridge.usage(payload)
                self.assertEqual(len(uses),1)
                self.assertEqual(uses[0][0],"native-fixture")
            finally:
                bridge.store.close()

    def test_signed_receipt_rejects_another_project_and_modified_payload(self):
        from hermes_codex_learning.read_receipts import initialize, issue, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);initialize(root)
            token=issue(root,{"name":"fixture","version":"v1","file_path":"SKILL.md","content_hash":"hash","scope":"worktree"},"/a")
            self.assertEqual(verify(root,token,"/a")["name"],"fixture")
            with self.assertRaises(PermissionError):verify(root,token,"/b")
            with self.assertRaises(PermissionError):verify(root,token[:-1]+("0" if token[-1]!="0" else "1"),"/a")
