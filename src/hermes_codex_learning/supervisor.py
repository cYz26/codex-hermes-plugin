"""A stdlib owner survives Hermes interpreter switches and startup failures."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import Settings
from .ipc import request
from .store import Store, atomic_json, digest
from .updates import ROOT, UpdateError, Updater, catalog, exclusive, read_json, verify_source

# These are the stores used by Learning API v1, native review and Curator.
# Never restore the hook outbox or any shared/root authentication store.
DATA_PATHS = ("config.yaml", "SOUL.md", "memories", "skills", "learning", "sessions", "state",
              ".curator_backups", "state.db", "state.db-wal", "state.db-shm")
DERIVED_KEYS = ("knowledge-index-v1", "publications-v1", "publication-decisions", "publication-context-version",
               "skill-versions-v1", "workflow-skill-attestations")


def copy_entry(source: Path, target: Path):
    if source.is_symlink():
        raise UpdateError(f"Learning data must not be a symlink: {source.name}")
    if source.is_dir():
        # A symlink inside a skill is preserved without following it outside home.
        shutil.copytree(source, target, symlinks=True,
                        ignore=shutil.ignore_patterns("owner.lock", "*.sock"))
    elif source.is_file():
        shutil.copy2(source, target)


def snapshot(settings: Settings, destination: Path):
    destination.mkdir(parents=True, mode=0o700)
    with exclusive(settings.state / "owner.lock"), exclusive(settings.home / "learning/owner.lock"):
        for name in DATA_PATHS:
            path = settings.home / name
            if path.exists() or path.is_symlink():
                copy_entry(path, destination / name)
        store = Store(settings.state)
        try:
            atomic_json(destination / "bridge-derived.json", {key: store.get(key) for key in DERIVED_KEYS})
        finally:
            store.close()
        atomic_json(destination / "complete.json", {"home": str(settings.home), "paths": list(DATA_PATHS)})


def restore(settings: Settings, source: Path):
    if read_json(source / "complete.json", {}).get("home") != str(settings.home):
        raise UpdateError("Incomplete or foreign learning snapshot")
    with exclusive(settings.state / "owner.lock"), exclusive(settings.home / "learning/owner.lock"):
        for name in DATA_PATHS:
            target = settings.home / name
            if name == "learning" and target.is_dir() and not target.is_symlink():
                # Keep the owner-lock inode while holding it so another worker
                # cannot acquire a replacement lock during restoration.
                for child in target.iterdir():
                    if child.name == "owner.lock":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                if (source / name).exists():
                    for child in (source / name).iterdir():
                        copy_entry(child, target / child.name)
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            if (source / name).exists():
                copy_entry(source / name, target)
        derived = read_json(source / "bridge-derived.json", {})
        store = Store(settings.state)
        try:
            for key in DERIVED_KEYS:
                if key in derived and derived[key] is not None:
                    store.set(key, derived[key])
                elif key in derived:
                    with store.db:
                        store.db.execute("DELETE FROM meta WHERE key=?", (key,))
        finally:
            store.close()


class Worker:
    def __init__(self, root: Path = ROOT):
        self.root, self.process, self.settings = root, None, None

    def start(self, settings: Settings, config: Path, *, paused=False):
        self.settings = settings
        self.interpreter = settings.interpreter
        env = {**os.environ, **settings.native_environment(), "HERMES_LEARNING_SUPERVISED": "1",
               "HERMES_LEARNING_START_PAUSED": "1" if paused else "0"}
        self.process = subprocess.Popen([str(self.interpreter), "-I", "-B", str(self.root / "scripts/worker.py"), str(config)],
                                        cwd=settings.project, env=env, stdin=subprocess.PIPE)

    def ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            try:
                value = request(self.settings, "learning_status")
                if (value.get("pid") == self.process.pid and value.get("ready")
                        and value.get("learning_api") == 1 and value.get("source") == str(self.settings.source)
                        and value.get("python") == str(self.interpreter)):
                    # Exercise the installed bridge read surface with its scoped knowledge policy.
                    request(self.settings, "context", {"cwd": str(self.settings.project)})
                    return
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(0.2)
        raise UpdateError("New learning worker did not pass startup/API/context checks")

    def drain(self, timeout=300):
        if self.process is None:
            return
        if self.process.poll() is not None:
            return
        request(self.settings, "quiesce_for_update")
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Never interrupt a real review just to install an update.
            raise UpdateError("Current native review is still draining; update deferred") from exc
        if self.process.returncode:
            raise UpdateError("Old worker did not close cleanly; update deferred")

    def resume(self):
        request(self.settings, "resume_learning")

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.process.stdin:
            self.process.stdin.close()


class Supervisor:
    def __init__(self, config: Path, *, root: Path = ROOT, worker=None):
        self.config, self.root = config.resolve(), root
        self.worker = worker or Worker(root)
        self.stop = threading.Event()
        self.updater = Updater(Settings.load(config), root=root)

    def recover(self):
        """An uncommitted activation never starts learning after a supervisor crash."""
        path = self.updater.directory / "transaction.json"
        transaction = read_json(path)
        if transaction and transaction["state"] == "committed":
            candidate = read_json(self.updater.directory / "candidate.json", {})
            current = Settings.load(self.config)
            if candidate.get("site") and candidate.get("source") == str(current.source):
                from .site_updates import publish_binding
                publish_binding(current, candidate, self.config)
            return
        if not transaction or transaction["state"] not in {"switching", "starting", "restoring"}:
            return
        current = read_json(self.config)
        if current not in (transaction["before"], transaction["after"]):
            raise UpdateError("Settings changed during interrupted activation; recovery needs inspection")
        transaction["state"] = "restoring"
        atomic_json(path, transaction)
        restore(self.updater.settings, Path(transaction["snapshot"]))
        atomic_json(self.config, transaction["before"])
        transaction.update(state="recovered", recovered_at=time.time())
        atomic_json(path, transaction)
        (self.updater.directory / "request.json").unlink(missing_ok=True)

    def activate(self):
        updater = Updater(Settings.load(self.config), root=self.root)
        with exclusive(updater.lock):
            request_path = updater.directory / "request.json"
            pending = read_json(request_path)
            if not pending:
                return
            if pending.get("automatic") and updater.settings.update_mode != "auto":
                pending["state"] = "cancelled_by_policy"
                atomic_json(updater.directory / "cancelled-request.json", pending)
                request_path.unlink()
                return
            before = read_json(self.config)
            before_policy = before.get("updates", {})
            if pending["action"] == "apply":
                candidate = read_json(updater.directory / "candidate.json", {})
                updater.validate_candidate(candidate)
                if candidate["id"] != pending["target"] or candidate.get("preflight_only"):
                    raise UpdateError("Requested candidate was replaced")
                target = {key: candidate[key] for key in ("source", "python")}
                binding = candidate.get("official_runtime")
                if binding:
                    target["fallback_python"] = candidate["fallback_python"]
                    target["updates"] = {**before_policy, "official_source": binding["source"],
                                         "upgrade_official": candidate.get("upgrade_official", False)}
                elif before.get("fallback_python"):
                    raise UpdateError("Shared mode requires a candidate built from the official installation")
            elif pending["action"] == "rollback":
                previous = read_json(updater.directory / "previous.json", {})
                if digest(previous) != pending["target"]:
                    raise UpdateError("Rollback target changed")
                recipe = updater.recipes().get(previous.get("tag"))
                if not recipe:
                    raise UpdateError("Rollback runtime no longer has a compatible recipe")
                updater.runtime_info(Path(previous["source"]))
                target = {key: previous[key] for key in ("source", "python")}
                target["fallback_python"] = previous.get("fallback_python")
                if before_policy.get("official_source") or previous.get("official_source"):
                    target["updates"] = {**before_policy, "official_source": previous.get("official_source"),
                                         "upgrade_official": previous.get("upgrade_official", False)}
            else:
                raise UpdateError("Invalid activation request")
            if all(before.get(key) == target[key] for key in target):
                request_path.unlink()
                return
            previous = {"tag": updater.current(), **{key: before.get(key) for key in ("source", "python", "fallback_python")},
                        **{key: before_policy[key] for key in ("official_source", "upgrade_official") if key in before_policy}}
            # Record exact settings so a concurrent edit is never overwritten.
            self.worker.drain()
            if read_json(self.config) != before:
                raise UpdateError("Settings changed while draining; update deferred")
            backup = updater.directory / "snapshots" / uuid.uuid4().hex
            snapshot(updater.settings, backup)
            if read_json(self.config) != before:
                raise UpdateError("Settings changed during snapshot; update deferred")
            if pending["action"] == "apply" and candidate.get("official_runtime"):
                updater.validate_candidate(candidate)
            transaction = {"id": pending["id"], "action": pending["action"], "state": "switching",
                           "before": before, "after": {**before, **target}, "snapshot": str(backup),
                           "started_at": time.time()}
            transaction_path = updater.directory / "transaction.json"
            atomic_json(transaction_path, transaction)
            try:
                atomic_json(self.config, transaction["after"])
                transaction["state"] = "starting"
                atomic_json(transaction_path, transaction)
                self.worker.start(Settings.load(self.config), self.config, paused=True)
                self.worker.ready()
                atomic_json(updater.directory / "previous.json", previous)
                # Commit BEFORE resuming. A crash after this point restarts the new
                # worker, never restores a snapshot over newly learned artifacts.
                transaction.update(state="committed", committed_at=time.time())
                atomic_json(transaction_path, transaction)
            except Exception as exc:
                self.worker.stop()
                transaction.update(state="restoring", error=f"{type(exc).__name__}: {exc}")
                atomic_json(transaction_path, transaction)
                if read_json(self.config) != transaction["after"]:
                    raise UpdateError("Settings edited during failed startup; recovery needs inspection") from exc
                restore(updater.settings, backup)
                atomic_json(self.config, before)
                transaction.update(state="rolled_back", restored_at=time.time())
                atomic_json(transaction_path, transaction)
                if pending["action"] == "apply":
                    candidate.update(state="failed_activation", error=transaction["error"])
                    atomic_json(updater.directory / "candidate.json", candidate)
                request_path.unlink(missing_ok=True)
                self.worker.start(Settings.load(self.config), self.config)
                self.worker.ready()
                updater.notice(transaction)
                return
            request_path.unlink(missing_ok=True)
            if pending["action"] == "apply" and candidate.get("site"):
                from .site_updates import publish_binding
                publish_binding(Settings.load(self.config), candidate, self.config)
            try:
                self.worker.resume()
            except (OSError, RuntimeError, ValueError):
                # The transaction is already committed. Restart that runtime if
                # the resume RPC was lost; never roll back post-commit knowledge.
                self.worker.stop()
                self.worker.start(Settings.load(self.config), self.config)
                self.worker.ready()
            updater.notice(transaction)

    def run(self):
        thread = None
        observed_official = None
        next_official_probe = 0.0
        next_worker_probe = 0.0
        next_site_probe = 0.0
        with exclusive(self.updater.directory / "supervisor.lock"):
            self.recover()
            try:
                while not self.stop.is_set():
                    self.updater = Updater(Settings.load(self.config), root=self.root)
                    updater = self.updater
                    if updater.settings.site_manifest and time.monotonic() >= next_site_probe:
                        from .site_updates import refresh_runtime
                        next_site_probe = time.monotonic() + 30
                        atomic_json(updater.directory / "site-runtime.json", refresh_runtime(updater.settings))
                    if ((self.worker.process is None or self.worker.process.poll() is not None)
                            and time.monotonic() >= next_worker_probe):
                        next_worker_probe = time.monotonic() + 30
                        try:
                            self.worker.stop()
                            self.worker.start(updater.settings, self.config)
                            self.worker.ready()
                            (updater.directory / "worker-error.json").unlink(missing_ok=True)
                        except Exception as exc:
                            self.worker.stop()
                            atomic_json(updater.directory / "worker-error.json",
                                        {"at": time.time(), "error_type": type(exc).__name__, "state": "unavailable"})
                    # Candidate builds run off the learning thread; failure or slow
                    # networking cannot delay capture, reviews or MCP reads.
                    if thread is None or not thread.is_alive():
                        if (updater.directory / "request.json").exists():
                            try:
                                self.activate()
                            except Exception as exc:
                                atomic_json(updater.directory / "activation-error.json",
                                            {"at": time.time(), "error": f"{type(exc).__name__}: {exc}"})
                                # Preserve failed requests for diagnosis, not a tight retry loop.
                                pending = updater.directory / "request.json"
                                if pending.exists():
                                    pending.replace(updater.directory / "failed-request.json")
                        checked = read_json(updater.status_path, {}).get("checked_at", 0)
                        enabled = updater.settings.update_mode != "off"
                        remote_due = enabled and time.time() - checked >= updater.settings.update_interval
                        installed_due = False
                        if enabled and updater.settings.official_source and not remote_due and time.monotonic() >= next_official_probe:
                            from .official_runtime import installation_identity

                            next_official_probe = time.monotonic() + 30
                            try:
                                identity = installation_identity(updater.settings.official_source)
                                previous_check = read_json(updater.directory / "official-status.json", {})
                                installed_due = (identity != observed_official or previous_check.get("state") == "official_pending")
                                observed_official = identity
                            except (OSError, UpdateError):
                                observed_official = None  # Retry after an official checkout move finishes.
                        if remote_due or installed_due:
                            def cycle(updater=updater, installed_only=installed_due):
                                try:
                                    updater.cycle(installed_only=installed_only, config=self.config)
                                except Exception as exc:
                                    path = updater.directory / "official-status.json" if installed_only else updater.status_path
                                    atomic_json(path, {"state": "check_failed", "checked_at": time.time(),
                                                "error": f"{type(exc).__name__}: {exc}"})
                            thread = threading.Thread(target=cycle, daemon=True)
                            thread.start()
                    self.stop.wait(2)
            finally:
                self.worker.stop()


def main(config: str) -> int:
    os.umask(0o077)
    supervisor = Supervisor(Path(config))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: supervisor.stop.set())
    supervisor.run()
    return 0
