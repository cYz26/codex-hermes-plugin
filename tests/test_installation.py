import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings


class InstallationTests(unittest.TestCase):
    def test_nondefault_configuration_pointer_is_shared_and_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root/"custom/bridge.json"
            config.parent.mkdir()
            data = {key:str(root/key) for key in ("home","project","source","python")}
            config.write_text(json.dumps(data))
            pointer = root/".hermes-codex-learning/installation.json"
            pointer.parent.mkdir()
            pointer.write_text(json.dumps({"schema_version":1,"settings":str(config)}))
            with patch("hermes_codex_learning.config.Path.home",return_value=root), patch.dict(os.environ,{},clear=True):
                self.assertEqual(Settings.load().home,root/"home")
                self.assertEqual(Settings.configuration_path(),config)
                override=root/"override.json"
                override.write_text(json.dumps({**data,"home":str(root/"other")}))
                with patch.dict(os.environ,{"HERMES_LEARNING_CONFIG":str(override)}):
                    self.assertEqual(Settings.load().home,root/"other")
                    self.assertEqual(Settings.load(config).home,root/"home")

    def test_bundle_is_immutable_and_changed_source_is_detected(self):
        from hermes_codex_learning.installation import build_bundle, verify_bundle
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"source"
            (source/"src").mkdir(parents=True)
            (source/"src/code.py").write_text("print('fixture')\n")
            target=root/"bundle"
            record=build_bundle(source,target)
            self.assertEqual(verify_bundle(target)["build"],record["build"])
            self.assertEqual(build_bundle(source,target)["build"],record["build"])
            (target/"src/code.py").write_text("changed")
            with self.assertRaises(ValueError):
                verify_bundle(target)

    def test_generated_install_metadata_does_not_change_or_enter_the_bundle(self):
        from hermes_codex_learning.identity import build_identity
        from hermes_codex_learning.installation import build_bundle
        from hermes_codex_learning.updates import plugin_hash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            code = source / "src/code.py"
            code.parent.mkdir(parents=True)
            code.write_text("print('fixture')\n")
            (source / ".codex-plugin").mkdir()
            (source / ".codex-plugin/plugin.json").write_text(json.dumps({"version": "0.1.0"}))
            (source / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
            (source / ".mcp.json").write_text('{}\n')
            expected = build_identity(source)
            fingerprint = plugin_hash(source)
            metadata = source / "src/fixture.egg-info/PKG-INFO"
            metadata.parent.mkdir()
            metadata.write_text("Metadata-Version: 2.4\nVersion: 0.4.2\n")
            self.assertEqual(build_identity(source), expected)
            self.assertEqual(plugin_hash(source), fingerprint)
            record = build_bundle(source, root / "bundle")
            self.assertEqual(record["build"], expected)
            self.assertEqual(plugin_hash(root / "bundle"), fingerprint)
            self.assertNotIn("src/fixture.egg-info/PKG-INFO", record["files"])
            self.assertFalse((root / "bundle/src/fixture.egg-info").exists())
            code.write_text("print('changed')\n")
            self.assertNotEqual(build_identity(source), expected)
            self.assertNotEqual(plugin_hash(source), fingerprint)
