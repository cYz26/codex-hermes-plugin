from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


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

    def native_environment(self) -> dict[str, str]:
        launch_home = self.profile_home or self.home
        if self.profile_home is not None and (
            launch_home.parent.name != "profiles" or launch_home.resolve() != self.home.resolve()
        ):
            raise ValueError("profile home must name this learning home under a Hermes profiles directory")
        return {"HERMES_HOME": str(launch_home), "HERMES_RUNTIME_DIR": str(self.home.parent / "tools")}

    @property
    def state(self) -> Path:
        return self.home / "bridge"

    @property
    def socket(self) -> Path:
        return self.state / "bridge.sock"

    def contains(self, cwd: str) -> bool:
        return Path(cwd).resolve().is_relative_to(self.project.resolve())

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
        )
