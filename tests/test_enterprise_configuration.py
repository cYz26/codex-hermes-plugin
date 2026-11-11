import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_codex_learning.config import Settings
from hermes_codex_learning.enterprise import boundary
from hermes_codex_learning.operations import execute


class EnterpriseConfigurationTests(unittest.TestCase):
    def test_preview_apply_and_restore_preserve_unrelated_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);home=root/"home";home.mkdir()
            model=home/"config.yaml";model.write_text("model: approved-enterprise-route\n")
            path=root/"bridge.json"
            original={"home":str(home),"project":str(root/"project"),"source":str(root/"source"),
                      "python":"/usr/bin/python3","capture_scope":"global","checked_publication":True,
                      "data_policy":{"enabled":True,"workflow_projection":True},"notifications":False}
            path.write_text(json.dumps(original))
            args=SimpleNamespace(command="enterprise-config",settings=str(path),apply=False,disable=False)
            self.assertFalse(execute(args)["applied"])
            self.assertEqual(json.loads(path.read_text()),original)
            self.assertFalse((home/"bridge/enterprise-boundary.json").exists())
            args.apply=True
            self.assertTrue(execute(args)["restart_required"])
            self.assertIsNotNone(boundary(Settings.load(path).data_policy))
            enabled=json.loads(path.read_text())
            self.assertEqual({k:v for k,v in enabled.items() if k!="data_policy"},
                             {k:v for k,v in original.items() if k!="data_policy"})
            execute(args)  # Rebinding must retain the original restoration point.
            args.disable=True;args.apply=False
            self.assertFalse(execute(args)["applied"])
            self.assertEqual(Settings.load(path).data_policy["mode"],"trusted_enterprise")
            args.apply=True;execute(args)
            self.assertEqual(json.loads(path.read_text()),original)
            self.assertEqual(model.read_text(),"model: approved-enterprise-route\n")
