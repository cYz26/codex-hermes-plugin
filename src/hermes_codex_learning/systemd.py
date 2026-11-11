"""User-service integration. Rendering never installs or starts a service."""
from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .store import atomic_text
from .ipc import request

LABEL = "hermes-learning-pilot"
ROOT = Path(__file__).resolve().parents[2]


def drain_service(settings: Settings, timeout: float = 300) -> None:
    """Freeze only the supervisor so it cannot restart a worker being drained."""
    from .updates import exclusive
    from .store import digest
    lock = settings.home.parent / "updates" / digest(str(settings.home))[:16] / "update.lock"
    with exclusive(lock):
        main_pid = int(subprocess.run(["systemctl","--user","show",LABEL+".service","-p","MainPID","--value"],
                                      capture_output=True,text=True,check=True,timeout=10).stdout.strip())
        def start(pid):
            return (Path("/proc")/str(pid)/"stat").read_text().rsplit(")",1)[1].split()[19]
        subprocess.Popen([sys.executable,"-I","-B",str(ROOT/"scripts/service_guard.py"),str(main_pid),start(main_pid),
                          str(os.getpid()),start(os.getpid()),str(time.monotonic()+timeout+80)],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        subprocess.run(["systemctl", "--user", "kill", "--kill-whom=main", "--signal=SIGSTOP", LABEL + ".service"],
                       check=True, timeout=10)
        try:
            request(settings, "quiesce_for_update")
            deadline = time.monotonic() + timeout
            with (settings.state / "owner.lock").open("a+") as owner:
                while True:
                    try:
                        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Learning review is still draining; service replacement deferred")
                        time.sleep(0.2)
                # systemd sends SIGCONT with termination to stopped processes.
                subprocess.run(["systemctl", "--user", "stop", LABEL + ".service"], check=True, timeout=70)
        except BaseException:
            subprocess.run(["systemctl", "--user", "kill", "--kill-whom=main", "--signal=SIGCONT", LABEL + ".service"],
                           check=False, timeout=10, capture_output=True)
            raise


def verify_service(settings: Settings, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = request(settings, "learning_status", timeout=2)
            if (status.get("ready") and status.get("source") == str(settings.source)
                    and status.get("python") == str(settings.interpreter)):
                from .identity import build_identity
                if status.get("build", {}).get("digest") == build_identity()["digest"]:
                    return status
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Service did not start the expected learning build/runtime")


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
    unit = render_unit(settings, path, plugin_root=Path(getattr(args, "plugin_root", ROOT)))
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
        previous = destination.read_text() if destination.exists() else None
        active = False
        if destination.exists():
            atomic_text(settings.state / (LABEL + ".service.previous"), destination.read_text())
            active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", candidate.name],
                                    capture_output=True).returncode == 0
        if active:
            drain_service(settings)
        try:
            if getattr(args, "binding", None):
                args.binding.apply()
            atomic_text(destination, unit)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", *([] if active else ["--now"]), candidate.name], check=True)
            if active:
                subprocess.run(["systemctl", "--user", "restart", candidate.name], check=True)
            verify_service(settings)
        except Exception:
            if getattr(args, "binding", None):
                args.binding.rollback()
            if previous is not None:
                subprocess.run(["systemctl","--user","stop",candidate.name],check=False,timeout=70)
                atomic_text(destination, previous)
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
                if active:
                    subprocess.run(["systemctl", "--user", "restart", candidate.name], check=True)
            else:
                subprocess.run(["systemctl","--user","disable","--now",candidate.name],check=False,timeout=70)
                destination.unlink(missing_ok=True)
                subprocess.run(["systemctl","--user","daemon-reload"],check=True)
            raise
    print(json.dumps({"candidate": str(candidate), "installed": args.start,
                      "unit": candidate.name, "path": str(destination) if args.start else None,
                      "start_requested": args.start}))
    return 0
