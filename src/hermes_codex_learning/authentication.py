"""Native profile binding and read-only auth diagnostics; no credential copying."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

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


def custom_provider_status(config: dict) -> dict:
    model = config.get("model") or {}
    provider = str(model.get("provider", ""))
    name = provider.split(":", 1)[1] if provider.startswith("custom:") else ""
    entry = (config.get("providers") or {}).get(name, {})
    endpoint = entry.get("base_url") or entry.get("url") or entry.get("api") or model.get("base_url") or ""
    url = urlsplit(endpoint)
    return {"provider": provider, "model": model.get("default"),
            "api_mode": entry.get("api_mode") or entry.get("transport") or model.get("api_mode"),
            "session_affinity_header": entry.get("session_affinity_header"),
            "auth_mode": "native_custom_provider", "logged_in": None,
            "source": "native_configuration", "configuration_valid": bool(name and url.scheme in {"http", "https"} and url.hostname),
            "endpoint_host": url.hostname, "model_call_verified": None}


def native_auth_status(settings: Settings) -> dict:
    # The public native status builder is read-only. The CLI `auth status` wrapper can
    # heal OAuth rows, so use the builder directly and export only non-secret fields.
    program = """import json, sys
from hermes_cli.config import load_config_readonly
from hermes_constants import get_default_hermes_root, get_hermes_home
config = load_config_readonly() or {}
if str(config.get('model', {}).get('provider', '')).startswith('custom:'):
    sys.path.insert(0, BRIDGE_SOURCE)
    from hermes_codex_learning.authentication import custom_provider_status
    safe = custom_provider_status(config)
else:
    from hermes_cli.auth import get_codex_auth_status
    status = get_codex_auth_status()
    safe = {key: status.get(key) for key in ('logged_in', 'token_expired', 'source', 'auth_mode', 'error')}
    safe.update(provider=config.get('model', {}).get('provider'), model=config.get('model', {}).get('default'))
safe.update(home=str(get_hermes_home()), auth_root=str(get_default_hermes_root()))
print(json.dumps(safe))
""".replace("BRIDGE_SOURCE", repr(str(Path(__file__).resolve().parents[1])))
    result = subprocess.run([str(settings.interpreter), "-B", "-c", program], cwd=settings.source,
                            env={**os.environ, **settings.native_environment()},
                            text=True, capture_output=True, check=True, timeout=30)
    return json.loads(result.stdout)
