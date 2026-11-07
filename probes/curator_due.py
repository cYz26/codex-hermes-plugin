"""Exercise the actual worker's scheduled Curator path in a fresh isolated home."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_codex_learning.config import Settings
from hermes_codex_learning import __version__
from hermes_codex_learning.ipc import request
from hermes_codex_learning.store import atomic_json


def check(settings: Settings, config_path: Path) -> dict:
    os.environ.update(settings.native_environment())
    sys.path.insert(0, str(settings.source))
    from agent import curator
    from hermes_cli.config import atomic_config_write
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from tools import skill_usage

    home = settings.home
    atomic_config_write(home / "config.yaml", {
        "gateway": {"standalone": True},
        "plugins": {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}},
        }},
        "curator": {"enabled": True, "consolidate": False},
    })
    plugin = home / "plugins" / "codex-learning-bridge"
    plugin.parent.mkdir()
    plugin.symlink_to(ROOT / "hermes-plugin", target_is_directory=True)
    now = datetime.now(timezone.utc)
    ages = {"recent-fixture": 1, "stale-fixture": 20, "archive-fixture": 40, "locked-fixture": 40}
    originals = {}
    for name in ages:
        skill = home / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: Isolated Curator probe fixture.\n---\n\nFixture {name}.\n")
        originals[name] = skill.read_bytes()
        skill_usage.record_created(name, agent_created=True)
    usage = skill_usage.load_usage()
    for name, days in ages.items():
        stamp = (now - timedelta(days=days)).isoformat()
        usage[name].update(created_at=stamp, last_used_at=stamp, last_activity_at=stamp, use_count=1)
    skill_usage.save_usage(usage)
    assert skill_usage.set_locked("locked-fixture", True)

    context = PluginContext(PluginManifest(name="codex-learning-bridge", source="user"),
                            PluginManager(scope_key=str(home)))
    api = context.learning(version=1)
    try:
        api.open({"host": "isolated-curator-fixture", "project": str(settings.project),
                  "chat": "idle-history", "executor": "Curator acceptance fixture"})
        curator.save_state({"last_run_at": now.isoformat(), "run_count": 0})
        not_due = api.maintenance()
        assert not_due["curator"] is None
        interval = curator.get_interval_hours()
        idle_seconds = curator.get_min_idle_hours() * 3600
        due = now - timedelta(hours=interval + 1)
        curator.save_state({"last_run_at": due.isoformat(), "run_count": 0})
        active = api.maintenance()
        assert active["curator"] is None and active["idle_seconds"] < idle_seconds
        assert curator.load_state()["run_count"] == 0
    finally:
        api.close()

    # Fixture only: give the isolated worker old activity and a due timestamp.
    # The real daemon poll then decides whether to run native maintenance.
    with sqlite3.connect(home / "learning/journal.sqlite") as db:
        assert db.execute("UPDATE scopes SET activity=?", (now.timestamp() - idle_seconds - 60,)).rowcount == 1
    with (home / "worker.log").open("wb") as log:
        worker = subprocess.Popen([str(settings.python), str(ROOT / "scripts/serve.py"), str(config_path)],
                                  cwd=settings.project, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = curator.load_state()
                if state["run_count"] == 1 and state["last_report_path"]:
                    break
                if worker.poll() is not None:
                    raise RuntimeError(f"Isolated Curator worker exited with {worker.returncode}")
                time.sleep(0.1)
            else:
                raise RuntimeError("Isolated worker did not complete its scheduled Curator pass")
            report_dir = Path(state["last_report_path"])
            assert report_dir.resolve().is_relative_to(home)
            report = json.loads((report_dir / "run.json").read_text())
            assert (report_dir / "REPORT.md").is_file()
            assert report["auto_transitions"]["archived"] == 1
            assert report["auto_transitions"]["marked_stale"] == 1
            assert report["counts"]["tool_calls_total"] == 0 and not report["llm_error"]
            final = {name: skill_usage.get_record(name)["state"] for name in ages}
            assert final == {"recent-fixture": "active", "stale-fixture": "stale",
                             "archive-fixture": "archived", "locked-fixture": "active"}
            archive = list((home / "skills/.archive").glob("archive-fixture*/SKILL.md"))
            assert len(archive) == 1 and archive[0].read_bytes() == originals["archive-fixture"]
            assert not (home / "skills/archive-fixture").exists()
            for name in set(ages) - {"archive-fixture"}:
                assert (home / "skills" / name / "SKILL.md").read_bytes() == originals[name]
            assert "locked-fixture" in skill_usage.locked_skill_names()
            # Allow the next ordinary poll before checking the recurrence gate.
            status_path = settings.state / "status.json"
            deadline = time.monotonic() + 5
            first = None
            while time.monotonic() < deadline:
                if status_path.exists():
                    version = status_path.stat().st_mtime_ns
                    if first is not None and version != first:
                        break
                    first = version
                time.sleep(0.1)
            else:
                raise RuntimeError("Worker did not publish a second idle poll")
            assert curator.load_state()["run_count"] == 1
            assert request(settings, "learning_status")["error"] is None
        finally:
            if worker.poll() is None:
                try:
                    request(settings, "shutdown")
                    worker.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    worker.terminate()
                    worker.wait(timeout=5)
    return {"status": "passed", "version": __version__, "input_provenance": "isolated_backdated_fixture",
            "worker_entry_sha256": hashlib.sha256((ROOT / "scripts/serve.py").read_bytes()).hexdigest(),
            "trigger": "scheduled_worker_poll",
            "not_due_skipped": True, "active_scope_skipped": True, "same_interval_not_repeated": True,
            "interval_hours": interval, "min_idle_seconds": idle_seconds, "run_count": state["run_count"],
            "transitions": report["auto_transitions"], "fixture_states": final, "archive_bytes_preserved": True,
            "lock_preserved": True, "native_model_calls": 0, "native_report_sha256": hashlib.sha256(
                (report_dir / "run.json").read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, help="Read source and interpreter paths from this bridge config")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = Settings.load(args.settings)
    with tempfile.TemporaryDirectory(prefix="hcl-curator-", dir="/tmp") as tmp:
        root = Path(tmp).resolve()
        project, home = root / "project", root / "home"
        project.mkdir()
        home.mkdir()
        settings = replace(base, home=home, project=project, profile_home=None, network_environment={},
                           capture_scope="project", notifications=False)
        path = root / "bridge.json"
        atomic_json(path, {"home": str(home), "project": str(project), "source": str(settings.source),
                           "python": str(settings.python), "notifications": False})
        result = check(settings, path)
    atomic_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
