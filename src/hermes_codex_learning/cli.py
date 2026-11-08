from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import NETWORK_ENV_KEYS, Settings
from .store import atomic_json

BASE = "b200f5bc60e31588ba2cb60e288029c4a54ba5fa"


def initialize(args) -> int:
    root = Path(__file__).resolve().parents[2]
    home, source = Path(args.home).expanduser().resolve(), Path(args.source).resolve()
    project, interpreter = Path(args.project).resolve(), Path(args.python).expanduser().absolute()
    from .updates import catalog, git, verify_source
    recipes = catalog(root)
    commit = git(source, "rev-parse", "HEAD")
    recipe = next((item for item in recipes.values() if item["base_commit"] == commit), None)
    if not recipe:
        raise ValueError("Hermes checkout needs a pinned compatibility recipe")
    verify_source(source, recipe)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    project.mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    if config.exists():
        raise FileExistsError("Pilot config already exists; use its native Hermes configuration commands")
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(source))
    from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
    from hermes_constants import account_default_hermes_home
    from .authentication import bind_profile_home
    atomic_config_write(config, {
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "model": {"provider": "openai-codex", "default": "gpt-6.1-sol", "api_mode": "codex_responses"},
        "agent": {"reasoning_effort": "max"},
        "auth": {"adopt_external_logins": False},
        "gateway": {"standalone": True},
        "memory": {"memory_enabled": True, "user_profile_enabled": True, "write_approval": False},
        "skills": {"write_approval": False},
        "auxiliary": {"background_review": {"enabled": True, "provider": "auto", "model": ""}},
        "display": {"memory_notifications": "on"},
        "plugins": {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}},
        }},
    })
    plugin = home / "plugins" / "codex-learning-bridge"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.symlink_to(root / "hermes-plugin", target_is_directory=True)
    profile = bind_profile_home(home, Path(args.profile_home) if args.profile_home else
                                account_default_hermes_home() / "profiles" / ".codex-learning-pilot")
    path = home / "bridge.json"
    atomic_json(path, {"home": str(home), "project": str(project), "source": str(source),
                       "capture_scope": args.capture_scope, "excluded_projects": [],
                       "python": str(interpreter), "profile_home": str(profile),
                       "network_environment": {key: os.environ[key] for key in NETWORK_ENV_KEYS if os.environ.get(key)},
                       "idle_seconds": 120, "idle_interval": 600, "notifications": True,
                       "updates": {"mode": "auto", "interval_seconds": 86400}})
    print(json.dumps({"settings": str(path), "effort": "max", "authentication": "native_profile_fallback", "service": "not_started"}))
    return 0


def launchd(args) -> int:
    settings = Settings.load(args.settings)
    root = Path(__file__).resolve().parents[2]
    label = "com.cy.hermes-codex-learning.pilot"
    destination = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if destination.exists() and not args.replace:
        raise FileExistsError(str(destination))
    settings.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        # The supervisor must be able to start even when a worker interpreter is gone.
        "Label": label, "ProgramArguments": [sys.executable, "-I", "-B", str(root / "scripts/serve.py"),
                                             str(Path(args.settings).resolve())],
        "WorkingDirectory": str(settings.project), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10, "ProcessType": "Background",
        "EnvironmentVariables": {**settings.native_environment(), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(settings.state / "daemon.log"), "StandardErrorPath": str(settings.state / "daemon-errors.log"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-learning-", dir=destination.parent) as staging:
        candidate = Path(staging) / destination.name
        candidate.write_bytes(plistlib.dumps(payload))
        candidate.chmod(0o600)
        candidate.replace(destination)
    print(destination)
    if args.start:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], check=True)
    return 0


def install_notifier() -> int:
    from .notify import APP_EXECUTABLE, APP_ID, APP_NAME, notifier_app

    app = notifier_app()
    source = Path(__file__).resolve().parents[2] / "macos/HermesLearningNotifier.swift"
    app.parent.mkdir(parents=True, exist_ok=True)
    if app.exists():
        installed = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if installed.get("CFBundleIdentifier") != APP_ID:
            raise FileExistsError(str(app))

    with tempfile.TemporaryDirectory(prefix=".hermes-learning-", dir=app.parent) as staging:
        candidate = Path(staging) / app.name
        binary = candidate / "Contents" / "MacOS" / APP_EXECUTABLE
        binary.parent.mkdir(parents=True)
        (candidate / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": APP_ID, "CFBundleExecutable": APP_EXECUTABLE,
            "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSMinimumSystemVersion": "12.0",
            "LSUIElement": True,
        }))
        target = f"{platform.machine()}-apple-macosx12.0"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-target", target, "-O", str(source), "-o", str(binary)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(candidate)], check=True)
        backup = Path(staging) / "previous.app"
        if app.exists():
            app.rename(backup)
        try:
            candidate.rename(app)
        except OSError:
            if backup.exists():
                backup.rename(app)
            raise

    subprocess.run(["/usr/bin/open", "-gnj", "-a", str(app), "--args", "--authorize"], check=True)
    print(app)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex–Hermes learning bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook")
    commands.add_parser("mcp")
    commands.add_parser("install-notifier", help="Build and register the local macOS learning notification app")
    update = commands.add_parser("update", help="Check, verify and update the isolated Hermes learning runtime")
    actions = update.add_subparsers(dest="update_action", required=True)
    for action in ("status", "check", "prepare", "apply", "rollback", "policy", "run"):
        sub = actions.add_parser(action)
        sub.add_argument("--settings")
        if action == "prepare":
            sub.add_argument("--version", help="Reviewed official stable tag; defaults to latest release")
            sub.add_argument("--official-source", type=Path, help="Share this native Hermes installation's dependencies")
            sub.add_argument("--upgrade-official", action="store_true", default=None,
                             help="Let automatic mode initiate a verified update of the official installation")
        if action in {"check", "run"}:
            sub.add_argument("--installed", action="store_true", help="Only check or follow the local official installation")
        if action == "policy":
            sub.add_argument("--mode", choices=["off", "check", "auto"], required=True)
            sub.add_argument("--interval-seconds", type=float, default=86400)
    init = commands.add_parser("init")
    init.add_argument("--home", default=str(Path.home() / ".hermes-codex-learning/pilot"))
    init.add_argument("--source", required=True)
    init.add_argument("--project", required=True)
    init.add_argument("--capture-scope", choices=["project", "global"], default="project",
                      help="global accepts all local main chats; project retains the pilot allowlist")
    init.add_argument("--python", required=True)
    init.add_argument("--profile-home", help="Root/profiles entry for native auth; defaults to hidden .codex-learning-pilot")
    service = commands.add_parser("launchd")
    service.add_argument("--settings", required=True)
    service.add_argument("--start", action="store_true")
    service.add_argument("--replace", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--settings")
    stop = commands.add_parser("stop")
    stop.add_argument("--settings")
    login = commands.add_parser("login")
    login.add_argument("--settings")
    auth_status = commands.add_parser("auth-status")
    auth_status.add_argument("--settings")
    args = parser.parse_args()
    if args.command == "update":
        from .updates import UpdateError, Updater, exclusive
        config = Path(args.settings or os.environ.get("HERMES_LEARNING_CONFIG",
                      Path.home() / ".hermes-codex-learning/pilot/bridge.json")).expanduser().resolve()
        updater = Updater(Settings.load(config))
        try:
            if args.update_action == "policy":
                from dataclasses import replace
                replace(updater.settings, update_mode=args.mode, update_interval=args.interval_seconds)
                with exclusive(updater.lock):
                    data = json.loads(config.read_text())
                    data["updates"] = {**data.get("updates", {}), "mode": args.mode, "interval_seconds": args.interval_seconds}
                    atomic_json(config, data)
                result = data["updates"]
            elif args.update_action == "status":
                result = updater.status()
            elif args.update_action == "check":
                result = updater.check(installed_only=args.installed)
            elif args.update_action == "run":
                result = updater.cycle(installed_only=args.installed, config=config)
            elif args.update_action == "prepare":
                tag = args.version
                official_source = args.official_source or updater.settings.official_source
                if args.upgrade_official and official_source is None:
                    raise UpdateError("--upgrade-official requires --official-source or an existing official binding")
                if tag is None and official_source:
                    from .updates import catalog, git

                    head = git(official_source.expanduser().resolve(), "rev-parse", "HEAD")
                    tag = next((tag for tag, recipe in catalog().items() if recipe["base_commit"] == head), None)
                    if tag is None:
                        raise UpdateError("Installed official commit needs a reviewed compatibility recipe")
                if tag is None:
                    checked = updater.check()
                    if checked["state"] not in {"available", "current"}:
                        raise UpdateError(checked.get("reason") or checked.get("error", checked["state"]))
                    tag = checked["latest"]["tag"]
                result = updater.prepare(tag, official_source=official_source.expanduser().resolve() if official_source else None,
                                         upgrade_official=args.upgrade_official)
            else:
                result = updater.request(args.update_action)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get("state") in {"failed", "check_failed", "adaptation_required",
                                                "mechanism_review_required", "plan_required"} else 0
        except (UpdateError, OSError, ValueError) as exc:
            print(json.dumps({"state": "blocked", "error": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "hook":
        from .capture import main as capture
        return capture()
    if args.command == "mcp":
        from .mcp import main as mcp
        return mcp()
    if args.command == "install-notifier":
        return install_notifier()
    if args.command == "init":
        return initialize(args)
    if args.command == "launchd":
        return launchd(args)
    if args.command in {"auth-status", "login"}:
        from .authentication import native_auth_status
        settings = Settings.load(args.settings)
        status = native_auth_status(settings)
        if args.command == "auth-status" or status.get("logged_in"):
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0
        return subprocess.run([str(settings.interpreter), "-m", "hermes_cli.main", "auth", "add", "openai-codex", "--type", "oauth", "--browser"],
                              cwd=settings.source, env={**os.environ, **settings.native_environment()}).returncode
    from .ipc import request
    method = "shutdown" if args.command == "stop" else "learning_status"
    print(json.dumps(request(Settings.load(args.settings), method), ensure_ascii=False, indent=2))
    return 0
