"""Borrow a committed Hermes PM environment without updating the App checkout."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# This updater's native stable-channel selection runs under its checkout lock.
# Review that contract on a newer host before allowing unattended App updates.
GUARDED_UPDATE_HOSTS = frozenset({"2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"})


_SELECT = """
import json, sys
from pathlib import Path
source = Path(sys.argv[1])
sys.path.insert(0, str(source))
from pm.environments import committed_venv, runtime_facts_path, venv_python
venv = committed_venv(source)
if venv is None:
    raise RuntimeError('No official dependency generation is committed')
fact = json.loads(runtime_facts_path(source).read_text(encoding='utf-8-sig'))['packages']['venv']
print(json.dumps({'python': str(venv_python(venv)), 'stamp': fact['stamp']}))
"""

_CHECK = """
import json, sys
from pathlib import Path
source = Path(sys.argv[1])
sys.path.insert(0, str(source))
from pm.install import venv_is_current
print(json.dumps(venv_is_current(project_root=source)))
"""


def inspect_official(source: Path, commit: str) -> dict:
    """Read the official selection and check it against the approved source and lock."""
    from .updates import UpdateError, file_hash, git

    source = source.resolve()

    def check_source():
        if git(source, "rev-parse", "HEAD") != commit:
            raise UpdateError("Official checkout is not on the approved Hermes commit")
        if git(source, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("Official Hermes checkout has tracked changes or an update in progress")

    env = {key: value for key, value in os.environ.items()
           if key in {"HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL"}}
    env["HERMES_HOME"] = str(source.parent)

    def probe(python: str, program: str):
        result = subprocess.run([python, "-I", "-B", "-c", program, str(source)], cwd=source,
                                env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise UpdateError("Cannot read the official Hermes dependency selection")
        return json.loads(result.stdout)

    check_source()
    selected = probe(sys.executable, _SELECT)
    python = Path(selected["python"])
    environment = python.parent.parent
    if (not python.is_absolute() or not os.access(python, os.X_OK)
            or not (environment.parent / ".lease-managed").is_file()):
        raise UpdateError("Official Hermes needs a committed, lease-managed Python environment")
    if probe(str(python), _CHECK) is not True:
        raise UpdateError("Official Hermes dependencies are not current for this checkout")
    if probe(sys.executable, _SELECT) != selected:
        raise UpdateError("Official Hermes dependencies changed during validation; retry preparation")
    check_source()
    return {"source": str(source), "commit": commit, **selected,
            "python_config": file_hash(environment / "pyvenv.cfg")}


def lease_official_python(python: Path) -> None:
    """Use the native PM lease protocol before any learning work starts."""
    from hermes_cli.runtime_state import lease_generation
    from pm.filesystem import lock_path

    environment = python.parent.parent.resolve()
    generation = environment.parent
    if (environment != Path(sys.prefix).resolve() or generation.parent.name != "environments"
            or not (generation / ".lease-managed").is_file()):
        raise RuntimeError("Worker did not start inside the recorded official dependency generation")
    lock = lock_path(generation.parent.parent / ".install.lock", timeout=10, create_dir=False)
    if lock is None:
        raise RuntimeError("Official dependency publication is busy")
    try:
        if not (environment / "pyvenv.cfg").is_file():
            raise RuntimeError("Official dependency generation is no longer installed")
        lease_generation(environment)  # The native API releases the lease at process exit.
    finally:
        os.close(lock)


def installation_identity(source: Path) -> tuple[str, int | None]:
    from .updates import git

    key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    facts = source.parent / "installs" / key / "facts.json"
    try:
        publication = facts.stat().st_mtime_ns
    except FileNotFoundError:
        publication = None
    return git(source, "rev-parse", "HEAD"), publication


def verify_update_target(target, tag: str, commit: str):
    if (target.channel != "stable" or target.repository.lower() != "nousresearch/hermes-agent"
            or target.ahead or target.commit != commit or target.version != tag.removeprefix("v")):
        raise ValueError("Hermes selected a different target than the tested official release")
    return target


def guarded_native_update(source: Path, before: str, tag: str, commit: str) -> int:
    """Run the official updater with a process-local check at target selection."""
    from .updates import git

    if before not in GUARDED_UPDATE_HOSTS or git(source, "rev-parse", "HEAD") != before:
        raise RuntimeError("The official updater host changed or its update contract has not been reviewed")
    sys.path.insert(0, str(source))
    sys.argv = ["hermes", "update", "--channel", "stable", "--yes"]
    from hermes_cli import source_releases
    from hermes_cli.main import PROJECT_ROOT, main

    if PROJECT_ROOT.resolve() != source:
        raise RuntimeError("Native updater resolved a different installation")
    resolve = source_releases.resolve_source_target

    def checked_target(channel, git_cmd=None, cwd=None, **kwargs):
        if channel != "stable" or cwd is None or Path(cwd).resolve() != source:
            raise ValueError("Native updater selected a different channel or installation")
        if git(source, "rev-parse", "HEAD") != before or git(source, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Official checkout changed before native update target selection")
        return verify_update_target(resolve(channel, git_cmd, cwd, **kwargs), tag, commit)

    source_releases.resolve_source_target = checked_target
    return main() or 0


def run_official_update(binding: dict, tag: str, commit: str, *, root: Path, env: dict, log: Path) -> None:
    from .updates import UpdateError

    source = Path(binding["source"])
    if binding["commit"] not in GUARDED_UPDATE_HOSTS:
        raise UpdateError("This official updater host needs a reviewed target-selection adapter")
    program = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_codex_learning.official_runtime import guarded_native_update
raise SystemExit(guarded_native_update(Path(sys.argv[2]), *sys.argv[3:]))
"""
    env = {**env, "HERMES_HOME": str(source.parent)}
    env.pop("HERMES_RUNTIME_DIR", None)  # The official installation owns its own tool store.
    with log.open("ab") as output:
        process = subprocess.run([binding["python"], "-I", "-B", "-c", program, str(root / "src"),
                                  str(source), binding["commit"], tag, commit], cwd=source, env=env,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    if process.returncode:
        raise UpdateError(f"Official Hermes updater exited {process.returncode}; see {log}")
