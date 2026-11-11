import hashlib
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from hermes_codex_learning.policy import admit, sharing_check
from hermes_codex_learning.store import atomic_json


def enterprise_fixture(root):
    config=root/"native.yaml";config.write_text("model: approved-enterprise-route\n")
    boundary=root/"boundary.json"
    atomic_json(boundary,{"schema_version":1,"authority":"operator_confirmed_enterprise_deployment",
                         "hostname":socket.gethostname(),"uid":os.getuid(),"model_config":str(config),
                         "model_config_sha256":hashlib.sha256(config.read_bytes()).hexdigest(),
                         "body_learning":True,"internal_sharing":True,"approved_at":1})
    return {"enabled":True,"mode":"trusted_enterprise","enterprise_boundary":str(boundary)},config


class EnterprisePolicyTests(unittest.TestCase):
    def test_internal_body_is_admitted_without_public_source_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            policy,_=enterprise_fixture(Path(directory))
            event={"hook_event_name":"UserPromptSubmit","session_id":"c","turn_id":"t","cwd":"/work",
                   "prompt":"Internal service invoice.reconcile failed; preserve the diagnosed retry rule."}
            result=admit(event,policy,"/work")
            self.assertTrue(result["allowed"])
            self.assertEqual(result["payload"],event)
            self.assertEqual(result["classification"],"approved")
            self.assertEqual(result["audience"],"enterprise")
            self.assertNotEqual(result["classification"],"public")

    def test_credentials_are_removed_without_discarding_the_rest_of_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            policy,_=enterprise_fixture(Path(directory))
            event={"prompt":"Keep this diagnosis. Bearer synthetic-sensitive-value","api_key":"synthetic-key"}
            result=admit(event,policy,"/work")
            self.assertTrue(result["allowed"])
            self.assertNotIn("synthetic-sensitive-value",json.dumps(result["payload"]))
            self.assertNotIn("synthetic-key",json.dumps(result["payload"]))
            self.assertIn("Keep this diagnosis",result["payload"]["prompt"])

    def test_model_change_and_explicit_restriction_remain_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            policy,config=enterprise_fixture(Path(directory))
            self.assertFalse(admit({"classification":"L4","prompt":"restricted"},policy,"/work")["allowed"])
            self.assertFalse(admit({"do_not_learn":True,"prompt":"restricted"},policy,"/work")["allowed"])
            config.write_text("model: different-route\n")
            self.assertFalse(admit({"prompt":"internal"},policy,"/work")["allowed"])

    def test_cookie_and_authorization_headers_are_scrubbed(self):
        with tempfile.TemporaryDirectory() as directory:
            policy,_=enterprise_fixture(Path(directory))
            result=admit({"Cookie":"session=synthetic-cookie", "prompt":
                         "Useful diagnosis\nCookie: session=synthetic-cookie\nAuthorization: Basic synthetic-basic\nKeep verification."},policy,"/work")
            self.assertTrue(result["allowed"])
            body=json.dumps(result["payload"])
            self.assertNotIn("synthetic-cookie",body)
            self.assertNotIn("synthetic-basic",body)
            self.assertIn("Useful diagnosis",body)
            self.assertIn("Keep verification",body)

    def test_internal_skill_is_not_relabelled_as_public(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);policy,_=enterprise_fixture(root)
            body="---\nlearning_scope: global_candidate\n---\nApplicability: internal deployment.\nUse https://service.internal/docs.\nLimits: enterprise environment. Verification: check results."
            path=root/"SKILL.md";path.write_text(body)
            value={"content":body,"version":"v1","resource_dir":str(root),"files":{"SKILL.md":hashlib.sha256(path.read_bytes()).hexdigest()}}
            self.assertFalse(sharing_check(value,["/project"],[])["allowed"])
            self.assertTrue(sharing_check(value,["/project"],[],enterprise=True)["allowed"])
            result=admit({"kind":"shared_candidate","resources":{"SKILL.md":body}},policy,"/project",purpose="share")
            self.assertTrue(result["allowed"])
            self.assertEqual(result["audience"],"enterprise")
