from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

NETWORK_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                              "http_proxy", "https_proxy", "all_proxy", "no_proxy"})
REVIEW_PROTOCOLS = frozenset({"strict_evidence", "native"})
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


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
    proactive: bool = False
    quality_checks: bool = False
    retention_enabled: bool = False
    review_retry_enabled: bool = True
    review_retry_limit: int = 3
    review_retry_delay: float = 300
    review_retry_max_delay: float = 3600
    review_timeout_seconds: float = 900
    shared_profile: Path | None = None
    shared_client_id: str | None = field(default=None, repr=False)
    scoped_storage: bool = False
    native_skill_projection: bool = True
    codex_skills_dir: Path | None = None
    review_protocol: str = "strict_evidence"
    review_reasoning_effort: str | None = None
    model_sync_enabled: bool = True
    review_fallback_models: tuple[str, ...] = ()
    allow_cross_project_shared_skill_edits: bool = False
    active_turn_timeout_seconds: float = 1800
    raw_event_retention_days: float = 7
    session_context_limit: int = 256
    claude_skills_dir: Path | None = None
    automatic_review_interval_seconds: float = 120

    def __post_init__(self):
        if self.shared_profile is not None or self.shared_client_id is not None:
            if (self.shared_profile is None or not self.shared_profile.is_absolute()
                    or self.shared_profile.resolve() == self.home.resolve()
                    or not re.fullmatch(r"[a-f0-9]{48}", self.shared_client_id or "")
                    or self.capture_scope != "global" or not self.checked_publication):
                raise ValueError("Shared learning requires an independent local profile, global capture and checked publication")
        if any(not isinstance(value, bool) for value in (self.proactive,self.quality_checks,self.retention_enabled,
                                                       self.review_retry_enabled,self.native_skill_projection)):
            raise ValueError("Learning and retention switches must be booleans")
        if isinstance(self.review_retry_limit, bool) or not isinstance(self.review_retry_limit, int) or not 1 <= self.review_retry_limit <= 10:
            raise ValueError("Review retries must be bounded between one and ten")
        for value in (self.review_retry_delay, self.review_retry_max_delay):
            if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not 1 <= value <= 86400:
                raise ValueError("Review retry delays must be finite values between one second and one day")
        if self.review_retry_max_delay < self.review_retry_delay:
            raise ValueError("Maximum retry delay must cover the initial delay")
        timeout = self.review_timeout_seconds
        if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
            raise ValueError("Learning review timeout must be between 1 and 3600 seconds")
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
        for key in ("codex_skills_dir", "claude_skills_dir"):
            directory = getattr(self, key)
            if directory is not None:
                target, owner = directory.resolve(), self.home.resolve()
                if not directory.is_absolute() or target.is_relative_to(owner) or owner.is_relative_to(target):
                    raise ValueError(f"{key} must be an independent absolute host skills directory")
        if self.codex_skills_dir is not None and self.claude_skills_dir is not None:
            codex, claude = self.codex_skills_dir.resolve(), self.claude_skills_dir.resolve()
            if codex.is_relative_to(claude) or claude.is_relative_to(codex):
                raise ValueError("Codex and Claude skills directories must be distinct")
        interval = self.automatic_review_interval_seconds
        if type(interval) not in (int, float) or not 0 <= interval <= 86400:
            raise ValueError("learning.automatic_review_interval_seconds must be between 0 and 86400")
        if self.review_protocol not in REVIEW_PROTOCOLS:
            raise ValueError("learning.review_protocol must be native or strict_evidence")
        if self.review_reasoning_effort is not None and (
                not isinstance(self.review_reasoning_effort, str)
                or self.review_reasoning_effort.strip().lower() not in REASONING_EFFORTS):
            raise ValueError("learning.review_reasoning_effort must be a valid reasoning effort level")
        if not isinstance(self.model_sync_enabled, bool):
            raise ValueError("learning.model_sync.enabled must be a boolean")
        if not isinstance(self.scoped_storage, bool):
            raise ValueError("learning.scoped_storage must be a boolean")
        if not isinstance(self.allow_cross_project_shared_skill_edits, bool):
            raise ValueError("learning.allow_cross_project_shared_skill_edits must be a boolean")
        if any(not isinstance(m, str) or not m.strip() for m in self.review_fallback_models):
            raise ValueError("learning.fallback_models must contain non-empty model strings")
        active_timeout = self.active_turn_timeout_seconds
        if type(active_timeout) not in (int, float) or not math.isfinite(active_timeout) or not 60 <= active_timeout <= 86400:
            raise ValueError("learning.active_turn_timeout_seconds must be between 60 and 86400 seconds")
        raw_days = self.raw_event_retention_days
        if type(raw_days) not in (int, float) or not math.isfinite(raw_days) or not 1 <= raw_days <= 90:
            raise ValueError("retention.raw_event_days must be between 1 and 90 days")
        if (isinstance(self.session_context_limit, bool) or not isinstance(self.session_context_limit, int)
                or not 256 <= self.session_context_limit <= 4096):
            raise ValueError("hook.session_context_limit must be an integer between 256 and 4096 bytes")

    @property
    def uses_scoped_storage(self) -> bool:
        return self.scoped_storage or self.shared_profile is not None

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
        # Ignore sticky/shared directories (such as /tmp) and directories or .git markers
        # owned by another UID so stray shared-host markers cannot merge unrelated workspaces.
        uid = os.getuid()
        for candidate in (path, *path.parents):
            try:
                cand_stat = candidate.stat()
                if cand_stat.st_uid != uid or (cand_stat.st_mode & stat.S_ISVTX):
                    continue
                marker = candidate / ".git"
                if marker.exists() and marker.lstat().st_uid == uid:
                    return str(candidate)
            except OSError:
                continue
        return str(path)  # Non-Git and projectless chats keep their actual host cwd.

    @classmethod
    def configuration_path(cls, path: str | Path | None = None) -> Path:
        explicit = path or os.environ.get("HERMES_LEARNING_CONFIG")
        if explicit:
            return Path(explicit).expanduser().absolute()
        pointer = Path.home() / ".hermes-codex-learning/installation.json"
        if pointer.exists():
            binding = json.loads(pointer.read_text())
            selected = binding.get("settings")
            if binding.get("schema_version") != 1 or not isinstance(selected, str) or not Path(selected).is_absolute():
                raise ValueError("Invalid installed learning configuration pointer")
            return Path(selected)
        return Path.home() / ".hermes-codex-learning/pilot/bridge.json"

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        config = cls.configuration_path(path)
        data = json.loads(config.read_text())
        shared = data.get("shared_learning") or {}
        projection = data.get("projection") or {}
        if not isinstance(projection, dict):
            raise ValueError("projection configuration must be an object")
        projection_enabled = projection.get("enabled", True)
        capture_scope = data.get("capture_scope", "project")
        checked_publication = data.get("checked_publication", False)
        resolved_home = Path(data["home"]).expanduser().resolve()
        default_root = (Path.home() / ".hermes-codex-learning").resolve()
        installed_cfg = None
        pointer = Path.home() / ".hermes-codex-learning/installation.json"
        if pointer.is_file() and not pointer.is_symlink():
            try:
                sel = json.loads(pointer.read_text()).get("settings")
                if isinstance(sel, str) and Path(sel).is_absolute():
                    installed_cfg = Path(sel).resolve()
            except (OSError, ValueError):
                pass
        is_host_owner = resolved_home.is_relative_to(default_root) or (
            installed_cfg is not None and config.resolve() == installed_cfg
        )
        if "codex_skills_dir" in projection:
            raw_skills_dir = projection["codex_skills_dir"]
            codex_skills_dir = Path(raw_skills_dir).expanduser().absolute() if raw_skills_dir else None
        elif projection_enabled is True and capture_scope == "global" and checked_publication is True and is_host_owner:
            codex_skills_dir = (Path.home() / ".agents" / "skills").absolute()
        else:
            codex_skills_dir = None
        return cls(
            home=resolved_home, project=Path(data["project"]).resolve(),
            source=Path(data["source"]).resolve(), python=Path(data["python"]).absolute(),
            idle_seconds=float(data.get("idle_seconds", 120)),
            idle_interval=float(data.get("idle_interval", 600)),
            notifications=bool(data.get("notifications", True)),
            profile_home=Path(data["profile_home"]).expanduser().absolute() if data.get("profile_home") else None,
            network_environment=data.get("network_environment", {}),
            capture_scope=capture_scope,
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
            checked_publication=checked_publication,
            site_manifest=Path(data["updates"]["site_manifest"]).resolve() if data.get("updates", {}).get("site_manifest") else None,
            runtime_extras=tuple(data.get("runtime_extras", [])),
            proactive=data.get("learning", {}).get("proactive", False),
            quality_checks=data.get("learning", {}).get("quality_checks", False),
            retention_enabled=data.get("retention", {}).get("enabled", False),
            review_retry_enabled=data.get("learning", {}).get("retries", {}).get("enabled", True),
            review_retry_limit=data.get("learning", {}).get("retries", {}).get("max_retries", 3),
            review_retry_delay=data.get("learning", {}).get("retries", {}).get("initial_delay_seconds", 300),
            review_retry_max_delay=data.get("learning", {}).get("retries", {}).get("max_delay_seconds", 3600),
            review_timeout_seconds=data.get("learning", {}).get("review_timeout_seconds", 900),
            shared_profile=Path(shared["profile"]).expanduser().resolve() if "profile" in shared else None,
            shared_client_id=shared.get("client_id"),
            scoped_storage=data.get("learning", {}).get("scoped_storage", False),
            native_skill_projection=projection_enabled,
            codex_skills_dir=codex_skills_dir,
            review_protocol=data.get("learning", {}).get("review_protocol", "strict_evidence"),
            review_reasoning_effort=(data.get("learning", {}).get("review_reasoning_effort") or None),
            model_sync_enabled=data.get("learning", {}).get("model_sync", {}).get("enabled", True),
            review_fallback_models=tuple(data.get("learning", {}).get("fallback_models", ())),
            allow_cross_project_shared_skill_edits=data.get("learning", {}).get("allow_cross_project_shared_skill_edits", False),
            active_turn_timeout_seconds=float(data.get("learning", {}).get("active_turn_timeout_seconds", 1800)),
            raw_event_retention_days=float(data.get("retention", {}).get("raw_event_days", 7)),
            session_context_limit=int(data.get("hook", {}).get("session_context_limit", 256)),
            claude_skills_dir=Path(projection["claude_skills_dir"]).expanduser() if projection.get("claude_skills_dir") else None,
            automatic_review_interval_seconds=data.get("learning", {}).get("automatic_review_interval_seconds", 120),
        )
