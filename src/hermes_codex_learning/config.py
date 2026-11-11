from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

NETWORK_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                              "http_proxy", "https_proxy", "all_proxy", "no_proxy"})


@dataclass(frozen=True)
class Settings:
    home: Path
    project: Path
    source: Path
    python: Path
    idle_seconds: float = 120
    idle_interval: float = 600
    notifications: bool = True
    profile_home: Path | None = None
    network_environment: dict[str, str] = field(default_factory=dict)
    capture_scope: str = "project"
    excluded_projects: tuple[str, ...] = ()
    update_mode: str = "auto"
    update_interval: float = 86400
    official_source: Path | None = None
    fallback_python: Path | None = None
    upgrade_official: bool = False
    auto_adapt: bool = False
    adaptation_timeout: float = 1800
    data_policy: dict = field(default_factory=dict)
    checked_publication: bool = False
    site_manifest: Path | None = None
    runtime_extras: tuple[str, ...] = ()

    def __post_init__(self):
        if self.capture_scope not in {"project", "global"}:
            raise ValueError("capture_scope must be project or global")
        if any(not Path(path).is_absolute() for path in self.excluded_projects):
            raise ValueError("excluded_projects must contain absolute paths")
        if self.update_mode not in {"off", "check", "auto"}:
            raise ValueError("updates.mode must be off, check or auto")
        if not 3600 <= self.update_interval <= 30 * 86400:
            raise ValueError("updates.interval_seconds must be between one hour and 30 days")
        if not isinstance(self.upgrade_official, bool) or (self.upgrade_official and self.official_source is None):
            raise ValueError("updates.upgrade_official requires an official source and a boolean value")
        if not isinstance(self.auto_adapt, bool) or not 60 <= self.adaptation_timeout <= 7200:
            raise ValueError("Automatic adaptation requires a boolean policy and a 60–7200 second timeout")
        if not isinstance(self.data_policy, dict) or ('workflow_projection' in self.data_policy
                and not isinstance(self.data_policy['workflow_projection'], bool)):
            raise ValueError('data_policy.workflow_projection must be a boolean')

    @property
    def interpreter(self) -> Path:
        # Native PM can collect a superseded generation while the supervisor
        # is down. The already-tested private interpreter serves the same source.
        if self.fallback_python is not None and (not self.python.is_file() or not os.access(self.python, os.X_OK)):
            return self.fallback_python
        return self.python

    @property
    def borrows_official_python(self) -> bool:
        return self.fallback_python is not None and self.interpreter == self.python

    def native_environment(self) -> dict[str, str]:
        launch_home = self.profile_home or self.home
        if self.profile_home is not None and (
            launch_home.parent.name != "profiles" or launch_home.resolve() != self.home.resolve()
        ):
            raise ValueError("profile home must name this learning home under a Hermes profiles directory")
        if (not isinstance(self.network_environment, dict) or set(self.network_environment) - NETWORK_ENV_KEYS
                or any(not isinstance(value, str) for value in self.network_environment.values())):
            raise ValueError("network_environment accepts only standard proxy environment strings")
        return {**self.network_environment, "HERMES_HOME": str(launch_home),
                "HERMES_RUNTIME_DIR": str(self.home.parent / "tools")}

    @property
    def state(self) -> Path:
        return self.home / "bridge"

    @property
    def socket(self) -> Path:
        return self.state / "bridge.sock"

    def contains(self, cwd: str) -> bool:
        return self.project_for(cwd) is not None

    def project_for(self, cwd: str | None) -> str | None:
        # Hook cwd is an actual local host path, never the MCP server's plugin cwd.
        if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
            return None
        path = Path(cwd).resolve()
        if any(path.is_relative_to(Path(excluded).resolve()) for excluded in self.excluded_projects):
            return None
        if path.is_relative_to(self.project.resolve()):
            return str(self.project.resolve())  # Preserve the pilot's existing identity.
        if self.capture_scope != "global":
            return None
        # A .git file is also a root (worktree/submodule). No shell or network lookup.
        for candidate in (path, *path.parents):
            if (candidate / ".git").exists():
                return str(candidate)
        return str(path)  # Non-Git and projectless chats keep their actual host cwd.

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        config = Path(path or os.environ.get("HERMES_LEARNING_CONFIG", Path.home() / ".hermes-codex-learning/pilot/bridge.json"))
        data = json.loads(config.read_text())
        return cls(
            home=Path(data["home"]).resolve(), project=Path(data["project"]).resolve(),
            source=Path(data["source"]).resolve(), python=Path(data["python"]).absolute(),
            idle_seconds=float(data.get("idle_seconds", 120)),
            idle_interval=float(data.get("idle_interval", 600)),
            notifications=bool(data.get("notifications", True)),
            profile_home=Path(data["profile_home"]).expanduser().absolute() if data.get("profile_home") else None,
            network_environment=data.get("network_environment", {}),
            capture_scope=data.get("capture_scope", "project"),
            excluded_projects=tuple(data.get("excluded_projects", [])),
            update_mode=data.get("updates", {}).get("mode", "auto"),
            update_interval=float(data.get("updates", {}).get("interval_seconds", 86400)),
            official_source=Path(data["updates"]["official_source"]).resolve()
                if data.get("updates", {}).get("official_source") else None,
            fallback_python=Path(data["fallback_python"]).absolute() if data.get("fallback_python") else None,
            upgrade_official=data.get("updates", {}).get("upgrade_official", False),
            auto_adapt=data.get("updates", {}).get("auto_adapt", False),
            adaptation_timeout=float(data.get("updates", {}).get("adaptation_timeout", 1800)),
            data_policy=data.get("data_policy", {}),
            checked_publication=data.get("checked_publication", False),
            site_manifest=Path(data["updates"]["site_manifest"]).resolve() if data.get("updates", {}).get("site_manifest") else None,
            runtime_extras=tuple(data.get("runtime_extras", [])),
        )
