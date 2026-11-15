"""Bind and run the local native client against a verified learning owner."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from .config import Settings
from .ipc import request
from .store import atomic_json, digest
from .updates import exclusive, file_hash, require_shared_source

MARKER = "shared-learning-client.json"


def add_parser(commands):
    parser = commands.add_parser("shared-learning", help="Prepare or use the local native Hermes client")
    actions = parser.add_subparsers(dest="shared_action", required=True)
    for name in ("bind", "inventory", "disconnect", "status", "cli", "desktop"):
        action = actions.add_parser(name)
        action.add_argument("--settings")
        if name in {"bind", "inventory"}:
            action.add_argument("--profile", required=True, type=Path)
        if name == "bind":
            action.add_argument("--apply", action="store_true")
        elif name == "inventory":
            continue
        elif name == "disconnect":
            action.add_argument("--apply", action="store_true")
        elif name == "status":
            action.add_argument("--cwd")
        elif name == "cli":
            import argparse
            action.add_argument("arguments", nargs=argparse.REMAINDER)
        else:
            action.add_argument("--app", type=Path, help="Hermes.app or its native executable")
            action.add_argument("--cwd", type=Path, help="Workspace for the Desktop Journey panel")


def _inventory(home):
    files, skills = {}, {}
    for top in ("memories", "skills", "pending", "shared-knowledge"):
        directory = home / top
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.rglob("*")):
            if (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory)
                    and not path.name.endswith((".lock", ".sock"))):
                relative = path.relative_to(home).as_posix()
                files[relative] = file_hash(path)
                if top == "skills" and path.name == "SKILL.md":
                    skills.setdefault(path.parent.name, []).append(path.parent.relative_to(home).as_posix() + "/")
    for name, packages in skills.items():
        skills[name] = sorted(digest({key[len(package):]: checksum for key, checksum in files.items()
                                      if key.startswith(package)}) for package in packages)
    return {"files": files, "skills": skills}


def _migration_inventory(owner, profile):
    existing, native = _inventory(owner), _inventory(profile)
    names = existing["skills"].keys() & native["skills"].keys()
    return {"owner": existing, "native": native, "same_name_skills": {
        name: "identical" if existing["skills"][name] == native["skills"][name] else "conflict"
        for name in sorted(names)}}


def _backup(settings, path, profile):
    from .supervisor import copy_entry

    root = settings.state / "shared-learning-backups" / secrets.token_hex(12)
    root.mkdir(parents=True, mode=0o700)
    for directory, name in ((settings.home, "owner"), (profile, "native")):
        backup = root / name
        backup.mkdir(mode=0o700)
        for relative in ("memories", "skills", "pending", "shared-knowledge"):
            copy_entry(directory / relative, backup / relative)
    atomic_json(root / "inventory.json", _migration_inventory(root / "owner", root / "native"))
    atomic_json(root / "bridge.json", json.loads(path.read_text()))
    return root


def _bind(settings, path, profile, *, apply=False):
    recipe = require_shared_source(settings.source)
    profile = profile.expanduser().resolve()
    if not profile.is_dir() or not (profile / "config.yaml").is_file():
        raise ValueError("Create the native execution profile with Hermes before binding it")
    if profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o022:
        raise PermissionError("The native profile must be owned by this account")
    if settings.shared_profile and profile != settings.shared_profile:
        raise ValueError("The owner is already bound to another execution profile")
    key = settings.shared_client_id or secrets.token_hex(24)
    replace(settings, shared_profile=profile, shared_client_id=key)
    marker = profile / MARKER
    if marker.is_symlink():
        raise PermissionError("The shared client marker must not be a symlink")
    view = {"state": "preview", "profile": str(profile), "owner": str(settings.home),
            "protocol": 1, "native_commit": recipe["base_commit"], "marker": str(marker)}
    if not apply:
        return view
    backup = None
    if settings.shared_profile is None:
        # This changes the owner's storage routing and must take effect at a worker boundary.
        with exclusive(settings.state / "owner.lock"), exclusive(settings.home / "learning" / "owner.lock"):
            document = json.loads(path.read_text())
            backup = _backup(settings, path, profile)
            document["shared_learning"] = {"profile": str(profile), "client_id": key}
            atomic_json(path, document)
    atomic_json(marker, {"version": 1, "profile": str(profile), "client_id": key, "socket": str(settings.socket)})
    return {**view, "state": "bound", "backup": str(backup) if backup else None,
            "next": "Start the normal learning worker and use a new native session"}


def _client_ready(settings):
    if not settings.shared_profile or not (settings.shared_profile / MARKER).is_file():
        raise ValueError("The Hermes client is not bound")
    require_shared_source(settings.source)
    try:
        proof = request(settings, "shared_hello", {"version": 1, "surface": "cli", "client": settings.shared_client_id}, timeout=2)
        if proof.get("protocol") != 1:
            raise RuntimeError("The owner is using an incompatible shared learning protocol")
    except OSError:
        pass  # A bound client can queue while the verified owner is offline.
    return settings.shared_profile


def _launch(args, settings):
    profile = _client_ready(settings)
    environment = {**os.environ, "HERMES_HOME": str(profile)}
    if args.shared_action == "cli":
        cli_args = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        if not cli_args:
            cli_args = ["chat"]
        start = ("import sys; sys.path.insert(0,sys.argv.pop(1)); "
                 "from hermes_cli.main import main; main()")
        os.execve(settings.interpreter, [str(settings.interpreter), "-I", "-B", "-c", start,
                                        str(settings.source), *cli_args], environment)
    if sys.platform != "darwin":
        raise ValueError("The Desktop launcher is for this Mac")
    default_source = settings.official_source or Path.home() / ".hermes/hermes-agent"
    roots = ([args.app] if args.app else [
        default_source / "apps/desktop/release/mac-arm64/Hermes.app",
        Path("/Applications/Hermes.app"), Path.home() / "Applications/Hermes.app",
    ])
    executables = [p / "Contents/MacOS/Hermes" if p.suffix == ".app" else p for p in roots]
    binary = next((p.expanduser().resolve() for p in executables if p.expanduser().is_file()), None)
    if not binary:
        raise ValueError("Hermes Desktop executable was not found; pass --app")
    processes = subprocess.check_output(["/bin/ps", "-axww", "-o", "comm="], text=True).splitlines()
    if any(item.strip() == str(binary) or item.strip().endswith("/Hermes.app/Contents/MacOS/Hermes")
           for item in processes):
        raise ValueError("Hermes Desktop is already running. Keep using it and load the new backend at the next normal launch.")
    recipe = require_shared_source(settings.source)
    stamp_path = binary.parent.parent / "Resources" / "install-stamp.json"
    if not stamp_path.is_file():
        raise ValueError("The installed Desktop has no Hermes build identity")
    stamp = json.loads(stamp_path.read_text())
    if stamp.get("commit") != recipe["base_commit"] or stamp.get("payload") == "bundled":
        raise ValueError("The installed Hermes Desktop version does not match the verified Python backend")
    launch_profile = "default"
    if profile.parent.name == "profiles":
        # The official profile picker resolves the bound profile from its existing Hermes root.
        environment["HERMES_HOME"] = str(profile.parent.parent)
        launch_profile = profile.name
    environment.update(HERMES_DESKTOP_HERMES_ROOT=str(settings.source),
                       HERMES_DESKTOP_PYTHON=str(settings.interpreter),
                       HERMES_DESKTOP_CDP_PORT="off")
    if args.cwd:
        project = settings.project_for(str(args.cwd.expanduser().resolve()))
        if project is None:
            raise ValueError("The Desktop workspace is excluded from learning")
        environment.update(HERMES_DESKTOP_CWD=project, HERMES_SHARED_LEARNING_MANAGEMENT_CWD=project)
    os.execve(binary, [str(binary), "--profile", launch_profile], environment)


def execute(args):
    path = Settings.configuration_path(args.settings).resolve()
    settings = Settings.load(path)
    action = args.shared_action
    if action == "bind":
        return _bind(settings, path, args.profile, apply=args.apply)
    if action == "inventory":
        return _migration_inventory(settings.home, args.profile.expanduser().resolve())
    if action == "disconnect":
        if not settings.shared_profile:
            return {"state": "disabled"}
        marker = settings.shared_profile / MARKER
        if args.apply and marker.exists():
            marker.replace(marker.with_name(MARKER + ".disabled"))
        return {"state": "disconnected" if args.apply else "preview", "profile": str(settings.shared_profile),
                "shared_knowledge": str(settings.home / "shared-knowledge")}
    if action == "status":
        if settings.shared_profile is None:
            return {"state": "disabled"}
        status = request(settings, "learning_status", {"cwd": args.cwd} if args.cwd else {}, timeout=3)
        return {"state": "bound" if (settings.shared_profile / MARKER).is_file() else "disconnected",
                "profile": str(settings.shared_profile), "owner": str(settings.home),
                "pid": status.get("pid"), "shared_learning": status.get("shared_learning")}
    return _launch(args, settings)
