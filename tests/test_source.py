import hashlib
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.source import verify_source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.base = "1" * 40
        self.archive = self.root / "upstream.tar.gz"
        self.archive.write_bytes(b"synthetic archive bytes")
        (self.source / "learning.py").write_text("verified patch\n")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"base_commit": self.base, "patch_sha256": "2" * 64,
            "files": {"learning.py": hashlib.sha256(b"verified patch\n").hexdigest()}}))
        (self.root / "source-provenance.json").write_text(json.dumps({
            "base_commit": self.base, "archive_name": self.archive.name,
            "archive_url": "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/" + self.base,
            "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        }))

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_and_patched_files_must_both_match(self):
        self.assertEqual(verify_source(self.source, self.manifest)["verified_files"], 1)
        (self.source / "learning.py").write_text("unexpected code")
        with self.assertRaisesRegex(ValueError, "file mismatch"):
            verify_source(self.source, self.manifest)

    def test_tampered_archive_is_rejected(self):
        self.archive.write_bytes(b"different upstream")
        with self.assertRaisesRegex(ValueError, "archive checksum"):
            verify_source(self.source, self.manifest)

    def test_source_provenance_cannot_point_outside_release(self):
        path = self.root / "source-provenance.json"
        value = json.loads(path.read_text())
        value["archive_name"] = "../upstream.tar.gz"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "provenance"):
            verify_source(self.source, self.manifest)

    def test_init_dry_run_selects_the_archive_release_without_creating_a_home(self):
        from hermes_codex_learning.cli import initialize

        native_cli = types.ModuleType("hermes_cli")
        native_cli.__path__ = []
        config = types.ModuleType("hermes_cli.config")
        config.DEFAULT_CONFIG = {"_config_version": 1}
        config.atomic_config_write = lambda *_: self.fail("Dry-run attempted to write native config")
        constants = types.ModuleType("hermes_constants")
        constants.account_default_hermes_home = lambda: self.root / "account"
        args = types.SimpleNamespace(home=str(self.root / "new-home"), source=str(self.source),
            project=str(self.root / "new-project"), python=sys.executable, profile_home=None,
            provider="custom:fixture", model="fixture-model", api_mode="codex_responses",
            base_url="https://provider.example.invalid/v1", reasoning_effort="max",
            session_affinity_header=None, dry_run=True, capture_scope="global")
        recipes = {"v0.22.7": {"base_commit": "3" * 40},
                   "v0.23.0": {"base_commit": self.base, "manifest_path": str(self.manifest)}}
        modules = {"hermes_cli": native_cli, "hermes_cli.config": config, "hermes_constants": constants}
        output = io.StringIO()
        original_path = list(sys.path)
        try:
            with patch("hermes_codex_learning.updates.catalog", return_value=recipes), \
                    patch.dict(sys.modules, modules), patch.dict(os.environ), contextlib.redirect_stdout(output):
                self.assertEqual(initialize(args), 0)
        finally:
            sys.path[:] = original_path
        result = json.loads(output.getvalue())
        self.assertEqual(result["source"]["base_commit"], self.base)
        self.assertEqual(result["source"]["provenance"], "official_archive")
        self.assertEqual(result["native_config"]["model"]["default"], "fixture-model")
        self.assertFalse(Path(args.home).exists())
        self.assertFalse(Path(args.project).exists())
