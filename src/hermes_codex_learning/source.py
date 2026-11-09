"""Verify the pinned native patch, including official archive-based installations."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_source(source: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    base = manifest["base_commit"]
    if (source / ".git").exists():
        check = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", base, "HEAD"],
                               capture_output=True, timeout=30)
        if check.returncode:
            raise ValueError("Hermes checkout does not contain the pinned stable baseline")
        provenance = "git"
    else:
        record = json.loads((source.parent / "source-provenance.json").read_text())
        name = record.get("archive_name", "")
        if (record.get("base_commit") != base or Path(name).name != name or not name
                or record.get("archive_url") != "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/" + base):
            raise ValueError("Archive provenance does not match the pinned upstream source")
        if _sha256(source.parent / name) != record.get("archive_sha256"):
            raise ValueError("Upstream source archive checksum mismatch")
        provenance = "official_archive"
    for relative, expected in manifest["files"].items():
        if _sha256(source / relative) != expected:
            raise ValueError("Pinned Learning API file mismatch: " + relative)
    return {"base_commit": base, "patch_sha256": manifest["patch_sha256"],
            "verified_files": len(manifest["files"]), "provenance": provenance}
