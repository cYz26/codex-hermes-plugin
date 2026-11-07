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
    check = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", BASE, "HEAD"])
    if check.returncode:
        raise ValueError("Hermes checkout does not match the approved stable baseline")
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
                       "idle_seconds": 120, "idle_interval": 600, "notifications": True})
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
        "Label": label, "ProgramArguments": [str(settings.python), str(root / "scripts/serve.py"), str(Path(args.settings).resolve())],
        "WorkingDirectory": str(settings.project), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10, "ProcessType": "Background",
        "EnvironmentVariables": {**settings.native_environment(), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(settings.state / "daemon.log"), "StandardErrorPath": str(settings.state / "daemon-errors.log"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plistlib.dumps(payload))
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
        return subprocess.run([str(settings.python), "-m", "hermes_cli.main", "auth", "add", "openai-codex", "--type", "oauth", "--browser"],
                              cwd=settings.source, env={**os.environ, **settings.native_environment()}).returncode
    from .ipc import request
    method = "shutdown" if args.command == "stop" else "learning_status"
    print(json.dumps(request(Settings.load(args.settings), method), ensure_ascii=False, indent=2))
    return 0
