"""Reproducible component identity: versions alone do not identify installed code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__
from .store import digest

ROOT = Path(__file__).resolve().parents[2]


def build_identity(root: Path = ROOT) -> dict:
    files = {}
    for name in ("src", "scripts", "hooks", "hermes-plugin", "patches", "skills", "desktop", "macos", "fixtures", "assets", "tests", "probes"):
        for path in sorted((root / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("pyproject.toml", ".mcp.json", ".codex-plugin/plugin.json"):
        path = root / name
        if path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = root/".codex-plugin/plugin.json"
    version = json.loads(manifest.read_text()).get("version") if manifest.is_file() else __version__
    return {"version": version, "digest": digest(files)}
