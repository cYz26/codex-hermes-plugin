"""Site overlays are verified separately from the clean official installation."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .store import atomic_json
from .updates import UpdateError, file_hash, git


def prepare_site(updater, recipe: dict, record: dict, interpreter: Path, command) -> None:
    manifest_path = updater.settings.site_manifest
    if manifest_path is None:
        return
    current = manifest_path.parent / "current-site.json"
    manifest = json.loads((current if current.exists() else manifest_path).read_text())
    if manifest.get("state") not in {"prepared", "verified"} or not manifest.get("source_tree"):
        raise UpdateError("Site migration needs sealed source and preserved regression tests")
    patch = Path(manifest.get("patch_path") or manifest_path.parent / "site.patch")
    if recipe.get("site_patch_file"):
        patch = Path(recipe["patch_path"]).parent / recipe["site_patch_file"]
        if not patch.resolve().is_relative_to(Path(recipe["patch_path"]).parent.resolve()):
            raise UpdateError("Site patch escapes the accepted adapter")
        expected = recipe.get("site_patch_sha256")
    else:
        if manifest["base_commit"] != recipe["base_commit"]:
            raise UpdateError("New official release needs a rebased site adapter")
        expected = manifest["patch_sha256"]
    if file_hash(patch) != expected:
        raise UpdateError("Site adapter patch changed")
    folder = Path(record["source"]).parent
    source = folder / "site-source"
    command("site-clone", ["git", "clone", "--quiet", "--no-hardlinks", record["source"], source])
    command("site-checkout", ["git", "-C", source, "reset", "--hard", recipe["base_commit"]])
    command("site-patch", ["git", "-C", source, "apply", "--index", patch])
    tests = manifest.get("tests", {})
    if not tests:
        raise UpdateError("Site adapter has no pinned regression suite")
    for name, checksum in tests.items():
        path = source / name
        if not path.resolve().is_relative_to(source.resolve()) or not path.is_file() or file_hash(path) != checksum:
            raise UpdateError(f"Site adapter changed a protected regression: {name}")
    tree = git(source, "write-tree")
    command("site-contracts", ["bash", "scripts/run_tests.sh", "-j", "2", *tests], cwd=source, timeout=1200)
    if git(source, "diff", "--name-only") or git(source, "write-tree") != tree:
        raise UpdateError("Site source changed during verification")
    record["site"] = {"source": str(source), "tree": tree, "patch_sha256": expected,
                      "base_commit": recipe["base_commit"], "tests": tests,
                      "patch_path": str(patch), "gateway_home": manifest.get("gateway_home"),
                      "gateway_service": manifest.get("gateway_service", "hermes-gateway.service"),
                      "binding": str(manifest_path.parent / "active-binding.json")}


def validate_site(record: dict) -> None:
    site = record.get("site")
    if not site:
        raise UpdateError("Candidate has no validated site overlay")
    source = Path(site["source"])
    if source != Path(record["source"]).parent / "site-source" or source.is_symlink():
        raise UpdateError("Site candidate path changed")
    if (git(source, "rev-parse", "HEAD") != site["base_commit"]
            or git(source, "diff", "--name-only") or git(source, "write-tree") != site["tree"]):
        raise UpdateError("Site source changed after validation")
    for name, checksum in site["tests"].items():
        if file_hash(source / name) != checksum:
            raise UpdateError("Site regression changed after verification")


def publish_binding(settings, candidate: dict, config: Path) -> None:
    if settings.site_manifest is None or not candidate.get("site"):
        return
    validate_site(candidate)
    site = candidate["site"]
    path = settings.site_manifest.parent / "active-binding.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    if (previous.get("state") == "ready" and previous.get("commit") == candidate["commit"]
            and previous.get("source_tree") == site["tree"] and previous.get("python") == candidate["python"]):
        return  # A learning-only patch does not restart an unchanged personal gateway.
    atomic_json(settings.site_manifest.parent / "active-binding.json", {
        "state": "ready", "source": site["source"], "source_tree": site["tree"],
        "python": candidate["python"], "fallback_python": candidate.get("fallback_python"),
        "official_source": str(settings.official_source), "commit": candidate["commit"],
        "learning_settings": str(config), "running_processes": "retain their pinned source until normal drain",
    })
    atomic_json(settings.site_manifest.parent / "current-site.json", {
        **site, "source_tree": site["tree"], "state": "verified", "base_commit": candidate["commit"],
    })


def refresh_runtime(settings) -> dict:
    """Use the native drain marker and SIGUSR1 contract; never interrupt busy work."""
    if settings.site_manifest is None:
        return {"state": "not_configured"}
    directory = settings.site_manifest.parent
    current = directory / "current-site.json"
    binding_file = directory / "active-binding.json"
    if not current.is_file() or not binding_file.is_file():
        return {"state": "awaiting_verified_binding"}
    site, binding = json.loads(current.read_text()), json.loads(binding_file.read_text())
    if not site.get("gateway_home"):
        return {"state": "new_processes_only"}
    home = Path(site["gateway_home"])
    unit = site.get("gateway_service", "hermes-gateway.service")
    if unit != "hermes-gateway.service":
        return {"state": "unsupported_service"}
    status_file = home / "gateway_state.json"
    if not status_file.is_file():
        return {"state": "gateway_not_running"}
    status = json.loads(status_file.read_text())
    pid = status.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return {"state": "unknown_gateway_identity"}
    try:
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return {"state": "foreign_gateway"}
        if binding["source"].encode() in (proc / "cmdline").read_bytes():
            return {"state": "ready", "pid": pid, "commit": binding["commit"]}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])).total_seconds()
        if age > 120 or status.get("gateway_state") not in {"running", "degraded"}:
            return {"state": "waiting_for_live_gateway"}
        if status.get("active_agents") != 0:
            return {"state": "waiting_for_idle"}
        service_pid = subprocess.run(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
                                     capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        command = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ExecStart", "--value"],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
        if service_pid != str(pid) or "site-launcher.py" not in command:
            return {"state": "gateway_service_not_bound"}
        marker = home / ".drain_request.json"
        if marker.exists():
            return {"state": "another_drain_is_active"}
        principal = "hermes-learning-update:" + uuid.uuid4().hex
        program = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
                   "from gateway.drain_control import write_drain_request\n"
                   "write_drain_request(home=Path(sys.argv[2]), principal=sys.argv[3], suppress_notification=True)\n")
        subprocess.run([str(settings.interpreter), "-I", "-B", "-c", program, str(settings.source), str(home), principal],
                       capture_output=True, timeout=20, check=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                observed = json.loads(status_file.read_text())
                if observed.get("pid") != pid:
                    return {"state": "gateway_changed"}
                if observed.get("gateway_state") == "draining" and observed.get("active_agents") == 0:
                    os.kill(pid, signal.SIGUSR1)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and proc.exists():
                        time.sleep(0.2)
                    return {"state": "restart_requested", "previous_pid": pid}
                time.sleep(0.2)
            return {"state": "drain_deferred"}
        finally:
            if marker.exists() and json.loads(marker.read_text()).get("principal") == principal:
                marker.unlink()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return {"state": "refresh_failed", "error_type": type(exc).__name__}
