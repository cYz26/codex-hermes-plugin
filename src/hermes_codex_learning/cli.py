from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import NETWORK_ENV_KEYS, Settings
from .store import atomic_json
from .configuration import build_native_config
from .source import verify_source

BASE = "b200f5bc60e31588ba2cb60e288029c4a54ba5fa"


def initialize(args) -> int:
    root = Path(__file__).resolve().parents[2]
    home, source = Path(args.home).expanduser().resolve(), Path(args.source).resolve()
    project, interpreter = Path(args.project).resolve(), Path(args.python).expanduser().absolute()
    from .updates import catalog, git, verify_source as verify_git_source
    git_source = (source / ".git").exists()
    if git_source:
        commit = git(source, "rev-parse", "HEAD")
    else:
        record = json.loads((source.parent / "source-provenance.json").read_text())
        if not isinstance(record, dict):
            raise ValueError("Archive provenance must be an object")
        commit = record.get("base_commit")
    recipe = next((value for value in catalog(root).values() if value["base_commit"] == commit), None)
    if recipe is None:
        raise ValueError("Hermes source needs a pinned compatibility recipe")
    if git_source:
        verify_git_source(source, recipe)
        provenance = {"base_commit": commit, "tag": recipe["tag"], "verified": True}
    else:
        provenance = verify_source(source, Path(recipe["manifest_path"]))
    provider = getattr(args, "provider", "openai-codex")
    if provider != "openai-codex" and args.profile_home:
        raise ValueError("--profile-home is only used for native OpenAI OAuth authentication")
    if not interpreter.is_file() or (project.exists() and not project.is_dir()):
        raise ValueError("A valid dedicated interpreter and project directory are required")
    config = home / "config.yaml"
    if config.exists():
        raise FileExistsError("Pilot config already exists; use its native Hermes configuration commands")
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(source))
    from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
    from hermes_constants import account_default_hermes_home
    from .authentication import bind_profile_home
    native = build_native_config(version=DEFAULT_CONFIG["_config_version"], provider=provider,
                                 model=getattr(args, "model", "gpt-6.1-sol"),
                                 api_mode=getattr(args, "api_mode", "codex_responses"),
                                 base_url=getattr(args, "base_url", None),
                                 reasoning_effort=getattr(args, "reasoning_effort", "max"),
                                 session_affinity_header=getattr(args, "session_affinity_header", None))
    auth_mode = "native_profile_fallback" if provider == "openai-codex" else "native_provider"
    if getattr(args, "dry_run", False):
        print(json.dumps({"dry_run": True, "home": str(home), "project": str(project),
                          "authentication": auth_mode, "native_config": native, "source": provenance}, ensure_ascii=False))
        return 0
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    project.mkdir(parents=True, exist_ok=True)
    atomic_config_write(config, native)
    plugin = home / "plugins" / "codex-learning-bridge"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.symlink_to(root / "hermes-plugin", target_is_directory=True)
    profile = None
    if provider == "openai-codex":
        profile = bind_profile_home(home, Path(args.profile_home) if args.profile_home else
                                    account_default_hermes_home() / "profiles" / ".codex-learning-pilot")
    path = home / "bridge.json"
    atomic_json(path, {"home": str(home), "project": str(project), "source": str(source),
                       "python": str(interpreter), "profile_home": str(profile) if profile else None,
                       "idle_seconds": 120, "idle_interval": 600, "notifications": sys.platform == "darwin",
                       "source_verification": provenance,
                       "capture_scope": args.capture_scope, "excluded_projects": [],
                       **({'data_policy': {'enabled': True, 'workflow_projection': True},
                           'checked_publication': True,
                           'learning': {'proactive': True, 'quality_checks': True},
                           'retention': {'enabled': True}} if args.capture_scope == 'global' else {}),
                       "network_environment": {key: os.environ[key] for key in NETWORK_ENV_KEYS if os.environ.get(key)},
                       "updates": {"mode": "auto", "interval_seconds": 86400}})
    print(json.dumps({"settings": str(path), "effort": native["agent"]["reasoning_effort"],
                      "authentication": auth_mode, "service": "not_started"}))
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
    from .macos_bundle import install_notification_app

    result = install_notification_app(Path(__file__).resolve().parents[2])
    print(result["app"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex–Hermes learning bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    from .operations import add_parsers
    add_parsers(commands)
    commands.add_parser("hook")
    commands.add_parser("mcp")
    notices = commands.add_parser("notices", help="UI-only SSH delivery; not exposed through MCP")
    notice_actions = notices.add_subparsers(dest="notice_action", required=True)
    for action in ("poll", "detail", "ack", "capabilities", "sync"):
        sub = notice_actions.add_parser(action)
        sub.add_argument("--settings")
        if action != "capabilities":
            sub.add_argument("--scope", required=action in {"detail", "ack"})
        if action in {"poll", "ack", "sync"}:
            sub.add_argument("--consumer", required=True)
        if action in {"detail", "ack"}:
            sub.add_argument("--id", required=True)
        if action == "ack":
            sub.add_argument("--stage", choices=["sent", "read"], required=True)
    commands.add_parser("install-notifier", help="Build and register the local macOS learning notification app")
    update = commands.add_parser("update", help="Check, verify and update the isolated Hermes learning runtime")
    actions = update.add_subparsers(dest="update_action", required=True)
    for action in ("status", "check", "prepare", "apply", "rollback", "policy", "run", "adapt"):
        sub = actions.add_parser(action)
        sub.add_argument("--settings")
        if action == "adapt":
            sub.add_argument("--version", required=True)
            sub.add_argument("--retry", action="store_true")
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
    init.add_argument("--provider", default="openai-codex")
    init.add_argument("--model", default="gpt-6.1-sol")
    init.add_argument("--api-mode", default="codex_responses")
    init.add_argument("--base-url")
    init.add_argument("--reasoning-effort", default="max")
    init.add_argument("--session-affinity-header")
    init.add_argument("--dry-run", action="store_true")
    linux = commands.add_parser("systemd", help="Render the Linux supervisor service; --start installs it")
    linux.add_argument("--settings", required=True)
    linux.add_argument("--start", action="store_true")
    linux.add_argument("--replace", action="store_true")
    linux.add_argument("--dry-run", action="store_true")
    doctor = commands.add_parser("doctor", help="Read configuration and runtime facts")
    doctor.add_argument("--settings")
    doctor.add_argument("--json", action="store_true")
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
    if args.command in {"install","sources","learning-config","enterprise-config","retention","quality","migration"}:
        from .operations import execute
        try:
            result = execute(args)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result.get("state") not in {"failed","pending"} else 2
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
            print(json.dumps({"state":"failed","error_type":type(exc).__name__,"error":str(exc)},ensure_ascii=False))
            return 2
    if args.command == "notices":
        from .notices import NoticeQueue
        queue = NoticeQueue(Settings.load(args.settings).state)
        try:
            if args.notice_action == "poll":
                result = queue.poll(args.consumer, args.scope)
            elif args.notice_action == "sync":
                raw = sys.stdin.buffer.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Notification request exceeds 64 KiB")
                body = json.loads(raw or "{}")
                result = queue.sync(args.consumer, after=body.get("after", 0), acknowledgements=body.get("acks", []))
            elif args.notice_action == "detail":
                result = queue.detail(args.id, args.scope)
            elif args.notice_action == "ack":
                result = queue.acknowledge(args.consumer, args.id, args.scope, args.stage)
            else:
                result = {"desktop_ssh": True, "chat_ui": False, "protocol_version": 2,
                          "chat_ui_reason": "No verified UI-only host entry point", "model_calls": 0}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        finally:
            queue.close()
    if args.command == "update":
        from .updates import UpdateError, Updater, exclusive
        config = Settings.configuration_path(args.settings).resolve()
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
            elif args.update_action == "adapt":
                from .adaptation import Adapter
                result = Adapter(updater).run(args.version, retry=args.retry)
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
                                                "mechanism_review_required", "plan_required", "adaptation_failed", "unknown"} else 0
        except (UpdateError, OSError, ValueError) as exc:
            print(json.dumps({"state": "blocked", "error": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "systemd":
        from .systemd import systemd
        return systemd(args)
    if args.command == "doctor":
        from .diagnostics import doctor
        return doctor(args)
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
        if args.command == "auth-status" or status.get("logged_in") or status.get("auth_mode") == "native_custom_provider":
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0
        return subprocess.run([str(settings.interpreter), "-m", "hermes_cli.main", "auth", "add", "openai-codex", "--type", "oauth", "--browser"],
                              cwd=settings.source, env={**os.environ, **settings.native_environment()}).returncode
    from .ipc import request
    method = "shutdown" if args.command == "stop" else "learning_status"
    print(json.dumps(request(Settings.load(args.settings), method), ensure_ascii=False, indent=2))
    return 0
