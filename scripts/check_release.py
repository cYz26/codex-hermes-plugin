#!/usr/bin/env python3
"""Offline checks for the publishable project; never runs a model or installs a plugin."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".venv", "backups", "build", "dist"}


def published_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_NAMES for part in relative.parts):
            continue
        if relative.parts[:2] == ("evidence", "private") or relative.name == "UPGRADE-CHECKPOINT.json":
            continue
        if path.is_file():
            yield path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_versions_and_patch():
    plugin = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    module = ast.parse((ROOT / "src/hermes_codex_learning/__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    assert plugin["version"] == package["project"]["version"] == version
    native_plugin = (ROOT / "hermes-plugin/plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*" + re.escape(version) + r"\s*$", native_plugin, re.M)
    manifest = json.loads((ROOT / "patches/manifest.json").read_text(encoding="utf-8"))
    patch = ROOT / "patches/native-learning-api-v1.patch"
    assert digest(patch) == manifest["patch_sha256"], "patch bytes differ from manifest"
    paths = re.findall(r"^diff --git a/(.+) b/\1$", patch.read_text(encoding="utf-8"), re.M)
    assert set(paths) == set(manifest["files"]) and len(paths) == len(set(paths))
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in paths)
    assert re.fullmatch(r"[a-f0-9]{40}", manifest["base_commit"])
    source = ast.parse((ROOT / "src/hermes_codex_learning/cli.py").read_text(encoding="utf-8"))
    base = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "BASE" for target in node.targets))
    assert base == manifest["base_commit"], "installer and patch target different Hermes bases"
    acceptance = json.loads((ROOT / "ACCEPTANCE.json").read_text(encoding="utf-8"))
    assert acceptance["upstream"]["base_commit"] == base
    assert acceptance["upstream"]["patch_sha256"] == manifest["patch_sha256"]
    assert acceptance["implementation_version"] == version
    assert digest(ROOT / "HERMES-CODEX-PROBE.json") == acceptance["historical_probe_sha256"]
    assert acceptance["verification"]["actual_app_hook_acceptance"] is False
    assert acceptance["real_provider_probe"]["input_provenance"] == "synthetic_acceptance_fixture_not_app_hooks"
    assert (ROOT / "scripts/bridge").stat().st_mode & 0o111, "hook launcher is not executable"
    for record in json.loads((ROOT / "evidence/INDEX.json").read_text(encoding="utf-8"))["records"]:
        if record["visibility"] == "repository":
            assert digest(ROOT / record["path"]) == record["sha256"], record["path"]


def check_links_and_credentials():
    errors = []
    patterns = {
        "private key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
        "API credential": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
        "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})\b"),
        "bearer credential": re.compile(r"(?i)Bearer\s+[A-Za-z0-9_./+-]{28,}"),
        "JWT credential": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    }
    for path in published_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule, pattern in patterns.items():
            if pattern.search(text):
                errors.append(f"{path.relative_to(ROOT)}: possible {rule}; value withheld")
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", text):
                target = unquote(target.strip("<>").split("#", 1)[0])
                if not target or "://" in target or target.startswith(("/", "#", "mailto:")):
                    continue
                if not (path.parent / target).exists():
                    errors.append(f"{path.relative_to(ROOT)}: missing local link {target}")
        if path.suffix == ".json":
            obj = json.loads(text)
            def find_live_receipts(value):
                if isinstance(value, dict):
                    if value.get("delivery_token"):
                        errors.append(f"{path.relative_to(ROOT)}: live delivery receipt in publishable JSON")
                    for item in value.values():
                        find_live_receipts(item)
                elif isinstance(value, list):
                    for item in value:
                        find_live_receipts(item)
            find_live_receipts(obj)
    if errors:
        raise RuntimeError("\n".join(errors))


def main():
    check_versions_and_patch()
    check_links_and_credentials()
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    if result.returncode:
        return result.returncode
    print("Release checks passed: package, patch, evidence, links, credential scan, and 9 bridge tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
