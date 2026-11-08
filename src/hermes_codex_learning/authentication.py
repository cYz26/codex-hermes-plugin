"""Native profile binding and read-only auth diagnostics; no credential copying."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import Settings


def bind_profile_home(home: Path, profile: Path) -> Path:
    profile = profile.expanduser().absolute()  # Keep the native root/profiles/name spelling.
    if profile.parent.name != "profiles":
        raise ValueError("profile home must be under a Hermes profiles directory")
    if profile.exists() or profile.is_symlink():
        if profile.resolve() != home.resolve():
            raise FileExistsError(f"profile home already belongs to another data directory: {profile}")
    else:
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.symlink_to(home.resolve(), target_is_directory=True)
    return profile


def native_auth_status(settings: Settings) -> dict:
    # The public native status builder is read-only. The CLI `auth status` wrapper can
    # heal OAuth rows, so use the builder directly and export only non-secret fields.
    program = """import json
from hermes_cli.auth import get_codex_auth_status
from hermes_constants import get_default_hermes_root, get_hermes_home
status = get_codex_auth_status()
safe = {key: status.get(key) for key in ('logged_in', 'token_expired', 'source', 'auth_mode', 'error')}
safe.update(home=str(get_hermes_home()), auth_root=str(get_default_hermes_root()))
print(json.dumps(safe))
"""
    result = subprocess.run([str(settings.interpreter), "-B", "-c", program], cwd=settings.source,
                            env={**os.environ, **settings.native_environment()},
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)
