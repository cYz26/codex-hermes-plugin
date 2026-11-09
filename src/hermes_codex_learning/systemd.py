"""User-service integration. Rendering never installs or starts a service."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .store import atomic_text

LABEL = "hermes-learning-pilot"
ROOT = Path(__file__).resolve().parents[2]


def _quoted(value: str | Path, *, command: bool = False) -> str:
    text = str(value)
    if any(char in text for char in ("\n", "\r", "\0")):
        raise ValueError("Service values cannot contain line breaks or NUL")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        text = text.replace("$", "$$")
    return '"' + text + '"'


def render_unit(settings: Settings, settings_path: Path, *, plugin_root: Path = ROOT) -> str:
    argv = [Path(sys.executable).absolute(), "-I", "-B", plugin_root / "scripts/serve.py", settings_path.absolute()]
    environment = {**settings.native_environment(), "PYTHONUNBUFFERED": "1"}
    # WorkingDirectory takes one path verbatim; unlike ExecStart it does not remove quotes.
    _quoted(settings.project)  # Reject newlines/NUL before writing a unit.
    directory = str(settings.project).replace("%", "%%")
    if directory != directory.strip():
        raise ValueError("Working directory cannot have leading or trailing whitespace")
    lines = [
        "[Unit]", "Description=Global Codex Hermes learning supervisor", "",
        "[Service]", "Type=simple", "WorkingDirectory=" + directory,
        "ExecStart=" + " ".join(_quoted(arg, command=True) for arg in argv),
        *("Environment=" + _quoted(key + "=" + value) for key, value in environment.items()),
        "UMask=0077", "Restart=on-failure", "RestartSec=5s", "TimeoutStopSec=60s", "",
        "[Install]", "WantedBy=default.target", "",
    ]
    return "\n".join(lines)


def systemd(args) -> int:
    path = Path(args.settings).expanduser().absolute()
    settings = Settings.load(path)
    unit = render_unit(settings, path)
    candidate = settings.state / (LABEL + ".service")
    destination = Path.home() / ".config/systemd/user" / candidate.name
    if getattr(args, "dry_run", False):
        print(json.dumps({"dry_run": True, "unit": candidate.name, "definition": unit,
                          "destination": str(destination)}))
        return 0
    if args.start and destination.exists() and not args.replace:
        raise FileExistsError(f"Service already exists; use --replace: {destination}")
    if args.start and not settings.project.is_dir():
        raise ValueError("Service working directory must exist before installation")
    atomic_text(candidate, unit)
    if args.start:
        subprocess.run(["systemd-analyze", "--user", "verify", str(candidate)], check=True)
        if destination.exists():
            atomic_text(settings.state / (LABEL + ".service.previous"), destination.read_text())
        atomic_text(destination, unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", candidate.name], check=True)
    print(json.dumps({"candidate": str(candidate), "installed": args.start,
                      "unit": candidate.name, "path": str(destination) if args.start else None,
                      "start_requested": args.start}))
    return 0
