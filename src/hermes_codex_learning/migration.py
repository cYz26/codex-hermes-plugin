"""Prepare a replayable site overlay without editing the running Hermes tree."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .store import atomic_json
from .updates import UpdateError, file_hash, git

_SECRET = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})|Bearer\s+[A-Za-z0-9._-]{24,}")


def changed_files(source: Path) -> list[str]:
    def names(args):
        result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
        return [name.decode() for name in result.stdout.split(b"\0") if name]
    return sorted(set(names(["diff", "--name-only", "-z", "HEAD"]) +
                      names(["ls-files", "--others", "--exclude-standard", "-z"])))


def source_inventory(source: Path) -> dict:
    result = {}
    for name in changed_files(source):
        path = source / name
        if path.is_symlink():
            result[name] = {"kind": "symlink"}
        elif path.is_file():
            result[name] = {"kind": "file", "sha256": file_hash(path)}
        elif not path.exists():
            result[name] = {"kind": "deleted"}
    return {"commit": git(source, "rev-parse", "HEAD"), "changes": result}


def prepare(source: Path, official: Path, output: Path) -> dict:
    source, official, output = source.resolve(), official.resolve(), output.absolute()
    if output.exists():
        raise FileExistsError("Migration output already exists; retain it and choose a new attempt")
    if git(official, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("Migration base must be the clean official checkout")
    before = source_inventory(source)
    output.mkdir(parents=True, mode=0o700)
    atomic_json(output / "source-inventory.json", before)
    candidate = output / "source"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(official), str(candidate)],
                   capture_output=True, check=True)
    conflicts, omitted, applied = [], [], []
    for name, info in before["changes"].items():
        local, target = source / name, candidate / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or info["kind"] == "symlink"
                or any(part in {".env", "auth.json", "credentials.json", ".npmrc", ".pypirc"} for part in Path(name).parts)):
            omitted.append({"path": name, "reason": "separate_configuration_or_symlink"})
            continue
        old = subprocess.run(["git", "-C", str(source), "show", "HEAD:" + name], capture_output=True)
        base = old.stdout if old.returncode == 0 else None
        content = local.read_bytes() if info["kind"] == "file" else None
        if content is not None and (len(content) > 4 * 1024 * 1024 or _SECRET.search(content.decode("utf-8", errors="replace"))):
            omitted.append({"path": name, "reason": "sensitive_or_large_content"})
            continue
        if content is None:
            if target.exists() and base is not None and target.read_bytes() != base:
                conflicts.append(name)
            else:
                target.unlink(missing_ok=True)
                applied.append(name)
            continue
        if not target.exists() or target.read_bytes() == base or target.read_bytes() == content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(local.stat().st_mode & 0o777)
            applied.append(name)
            continue
        if base is None:
            conflicts.append(name)
            continue
        ancestor = output / "ancestors" / name
        ours = output / "local" / name
        ancestor.parent.mkdir(parents=True, exist_ok=True)
        ours.parent.mkdir(parents=True, exist_ok=True)
        ancestor.write_bytes(base)
        ours.write_bytes(content)
        merged = subprocess.run(["git", "merge-file", "-p", str(ours), str(ancestor), str(target)], capture_output=True)
        if merged.returncode == 0:
            target.write_bytes(merged.stdout)
            applied.append(name)
        else:
            conflict = output / "conflicts" / name
            conflict.parent.mkdir(parents=True, exist_ok=True)
            conflict.write_bytes(merged.stdout)
            conflicts.append(name)
    if source_inventory(source) != before:
        raise UpdateError("Running source changed during migration preparation")
    subprocess.run(["git", "-C", str(candidate), "add", "-A"], capture_output=True, check=True)
    with (output / "site.patch").open("wb") as stream:
        subprocess.run(["git", "-C", str(candidate), "diff", "--cached", "--binary"], stdout=stream, check=True)
    record = {"state": "conflicts" if conflicts else "review_required" if omitted else "prepared",
              "source": str(source), "official_source": str(official), "candidate": str(candidate),
              "base_commit": git(official, "rev-parse", "HEAD"), "original_commit": before["commit"],
              "applied": applied, "conflicts": conflicts, "omitted": omitted,
              "patch_sha256": file_hash(output / "site.patch"), "created_at": time.time(),
              "source_unchanged": True, "activated": False}
    atomic_json(output / "migration.json", record)
    return record


def seal(output: Path) -> dict:
    """Seal reviewed conflict resolutions; never implies runtime verification."""
    record = json.loads((output / "migration.json").read_text())
    source, candidate = Path(record["source"]), Path(record["candidate"])
    resolutions = json.loads((output / "resolutions.json").read_text()) if record["conflicts"] else {}
    if source_inventory(source) != json.loads((output / "source-inventory.json").read_text()):
        raise UpdateError("Original Hermes changes no longer match the migration snapshot")
    for name in record["conflicts"]:
        path = candidate / name
        resolved = resolutions.get(name, {})
        if not resolved.get("reason") or not path.is_file() or resolved.get("sha256") != file_hash(path):
            raise UpdateError(f"Migration conflict has no matching reviewed resolution: {name}")
        if path.is_file() and re.search(rb"(?m)^(<<<<<<< |=======|>>>>>>> )", path.read_bytes()):
            raise UpdateError(f"Unresolved migration conflict: {name}")
    subprocess.run(["git", "-C", str(candidate), "add", "-A"], capture_output=True, check=True)
    with (output / "site.patch").open("wb") as stream:
        subprocess.run(["git", "-C", str(candidate), "diff", "--cached", "--binary"], stdout=stream, check=True)
    record.update(state="prepared", patch_sha256=file_hash(output / "site.patch"),
                  source_tree=git(candidate, "write-tree"), sealed_at=time.time())
    atomic_json(output / "migration.json", record)
    return record
