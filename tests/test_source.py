import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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
