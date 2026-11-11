"""Immutable plugin bundles and reversible per-user learning bindings."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from .config import Settings
from .identity import build_identity
from .store import atomic_json, atomic_text

SKIP = {".git","__pycache__",".pytest_cache",".ruff_cache",".venv","build","dist","backups"}


def files(root):
    if (root/".git").exists():
        names = subprocess.check_output(["git","-C",str(root),"ls-files","--cached","--others","--exclude-standard","-z"]).split(b"\0")
        paths = [root/os.fsdecode(name) for name in names if name]
    else:
        paths = list(root.rglob("*"))
    for path in sorted(set(paths)):
        relative = path.relative_to(root)
        if any(part in SKIP or part.endswith(".egg-info") for part in relative.parts) or relative.parts[:2] == ("evidence","private"):
            continue
        if path.name == "BUILD.json" or path.name == ".env" or path.name.startswith(".env."):
            continue
        if path.is_symlink():
            raise ValueError("A published bundle cannot contain symlinks: " + str(relative))
        if path.is_file():
            yield path


def verify_bundle(root):
    value = json.loads((root/"BUILD.json").read_text())
    actual = {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files(root)}
    if actual != value["files"] or build_identity(root) != value["build"]:
        raise ValueError("Installed plugin differs from its immutable build manifest")
    return value


def build_bundle(root, destination):
    root, destination = Path(root).resolve(), Path(destination).absolute()
    build = build_identity(root)
    if destination.exists():
        existing = verify_bundle(destination)
        if existing["build"] != build:
            raise FileExistsError("Refusing to replace a different installed build")
        return existing
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-",dir=destination.parent) as temp:
        stage = Path(temp)/"plugin"
        stage.mkdir()
        checksums = {}
        for path in files(root):
            relative = path.relative_to(root)
            target = stage/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
            checksums[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
        if build_identity(stage) != build:
            raise ValueError("Source changed during bundle preparation")
        record = {"schema_version":1,"build":build,"files":checksums}
        atomic_json(stage/"BUILD.json",record)
        stage.rename(destination)
    return verify_bundle(destination)


def swap_link(path, target):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex)
    temporary.symlink_to(target,target_is_directory=True)
    temporary.replace(path)


class Binding:
    def __init__(self, settings, config, root):
        self.settings, self.config, self.root = settings, config, root
        self.pointer = Path.home()/".hermes-codex-learning/installation.json"
        self.plugin = settings.home/"plugins/codex-learning-bridge"
        self.old_pointer = self.pointer.read_text() if self.pointer.exists() else None
        if self.plugin.exists() and not self.plugin.is_symlink():
            raise ValueError("Native plugin path is an unmanaged directory")
        self.old_plugin = os.readlink(self.plugin) if self.plugin.is_symlink() else None

    def apply(self):
        swap_link(self.plugin,self.root/"hermes-plugin")
        atomic_json(self.pointer, {"schema_version":1,"settings":str(self.config),
                                  "root":str(self.root),"build":build_identity(self.root)})

    def rollback(self):
        if self.old_plugin is not None:
            swap_link(self.plugin,self.old_plugin)
        else:
            self.plugin.unlink(missing_ok=True)
        if self.old_pointer is not None:
            atomic_text(self.pointer,self.old_pointer)
        else:
            self.pointer.unlink(missing_ok=True)


def install(args):
    config = Settings.configuration_path(args.settings)
    settings = Settings.load(config)
    root = Path(__file__).resolve().parents[2]
    build = build_identity(root)
    destination = settings.home.parent/"releases"/build["version"]/("plugin-"+build["digest"][:16])
    plan = {"settings":str(config),"bundle":str(destination),"build":build,"start_requested":args.start,
            "platform":sys.platform,"codex_plugin":"hermes-learning@hermes-learning-local"}
    if not args.apply:
        return {**plan,"dry_run":True}
    build_bundle(root,destination)
    binding = Binding(settings,config,destination)
    if args.start:
        if sys.platform != "linux":
            raise ValueError("Use the bundled launchd entry on macOS; this managed start is for Linux")
        from .systemd import systemd
        systemd(SimpleNamespace(settings=str(config),start=True,replace=True,dry_run=False,
                                plugin_root=destination,binding=binding))
    else:
        from .ipc import request
        try:
            request(settings,"learning_status",timeout=0.2)
        except (OSError,RuntimeError):
            binding.apply()
        else:
            raise RuntimeError("Use --start to drain and replace an active learning service")
    return {**plan,"installed":True,"codex_plugin_install_required":True}
