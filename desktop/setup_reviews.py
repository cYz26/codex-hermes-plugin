#!/usr/bin/env python3
"""Install review assets for an existing SSH receiver; preserve recoverable copies."""
import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

FILES = ("review.py", "review.html", "review.command")


def rollback_assets(state, record):
    previous = set(record["previous_files"])
    for name in FILES:
        path = state / name
        if name in previous:
            shutil.copy2(Path(record["backup"]) / name, path)
        else:
            path.unlink(missing_ok=True)


def install_assets(config_path, root):
    config_path, root = Path(config_path).resolve(), Path(root).resolve()
    config = json.loads(config_path.read_text())
    state = Path(config["state"]).expanduser().resolve()
    if state != config_path.parent:
        raise ValueError("The receiver configuration must be stored in its managed state directory")
    previous = [name for name in FILES if (state / name).exists()]
    if any((state / name).is_symlink() for name in FILES):
        raise ValueError("Review assets cannot replace symbolic links")
    backup = state / "backups" / ("reviews-" + str(time.time_ns())) if previous else None
    if backup:
        backup.mkdir(parents=True, mode=0o700)
        for name in previous:
            shutil.copy2(state / name, backup / name)
    record = {"files": list(FILES), "previous_files": previous, "backup": str(backup) if backup else None,
              "configuration_unchanged": True}
    try:
        with tempfile.TemporaryDirectory(prefix=".reviews-", dir=state) as temporary:
            stage = Path(temporary)
            for name in ("review.py", "review.html"):
                shutil.copy2(root / "desktop" / name, stage / name)
                (stage / name).chmod(0o600)
            command = "#!/bin/sh\nexec " + shlex.join([
                sys.executable, "-B", str(state / "review.py"), "--config", str(config_path), "--cwd"
            ]) + ' "$@"\n'
            (stage / "review.command").write_text(command)
            (stage / "review.command").chmod(0o700)
            for name in FILES:
                (stage / name).replace(state / name)
    except BaseException:
        rollback_assets(state, record)
        raise
    return record


def main():
    parser = argparse.ArgumentParser(description="Add the review page to the existing Mac SSH notifier")
    parser.add_argument("--config", type=Path, default=Path.home() / "Library/Application Support/HermesLearningSSH/config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run the installer on the Mac that runs Codex Desktop")
    root = Path(__file__).resolve().parents[1]
    config = json.loads(args.config.read_text())
    state = Path(config["state"]).expanduser().resolve()
    binary = Path(config["notifier"]).expanduser()
    if not binary.is_absolute() or len(binary.parents) < 3:
        raise ValueError("The receiver must name its actual notifier executable")
    app = binary.parents[2]
    if args.dry_run:
        print(json.dumps({"state": "preview", "assets": list(FILES), "app": str(app),
                          "config": str(args.config), "receiver_restart": False}, ensure_ascii=False))
        return 0
    os.umask(0o077)
    sys.path.insert(0, str(root / "src"))
    from hermes_codex_learning.macos_bundle import install_notification_app
    from hermes_codex_learning.store import atomic_json
    assets = None
    report = {"state": "installing", "native_ui_acceptance": False}
    try:
        assets = install_assets(args.config, root)
        report["notification_app"] = install_notification_app(root, app)
        report.update(state="installed", assets=assets, receiver_restart=False)
    except Exception as error:
        if assets:
            rollback_assets(state, assets)
        report.update(state="failed", error_type=type(error).__name__, error=str(error))
        atomic_json(state / "review-setup-report.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 2
    atomic_json(state / "review-setup-report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
