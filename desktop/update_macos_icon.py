#!/usr/bin/env python3
"""Update the installed notifier's icon without replacing the SSH receiver."""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def update_icon(app: Path, icon: Path, *, dry_run: bool = False) -> dict:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "com.cy.hermes-learning-notifier":
        raise ValueError("目标不是 Hermes 学习通知 App")
    icon_bytes = icon.read_bytes()
    destination = app / "Contents/Resources" / icon.name
    unchanged = (info.get("CFBundleIconFile") == icon.name and destination.is_file()
                 and destination.read_bytes() == icon_bytes)
    result = {"app": str(app), "icon": str(destination), "changed": not unchanged,
              "visibility": "requires_user_verification"}
    if dry_run:
        return {**result, "dry_run": True}

    register = ["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                "-f", str(app)]
    if unchanged:
        subprocess.run(register, check=True)
        return result

    os.umask(0o077)
    backup = app.parent / (".HermesLearning.icon-backup." + str(time.time_ns()))
    with tempfile.TemporaryDirectory(prefix=".hermes-learning-icon-", dir=app.parent) as temporary:
        candidate = Path(temporary) / app.name
        shutil.copytree(app, candidate, symlinks=True)
        resources = candidate / "Contents/Resources"
        resources.mkdir(exist_ok=True)
        shutil.copy2(icon, resources / icon.name)
        info["CFBundleIconFile"] = icon.name
        info["CFBundleVersion"] = str(int(info.get("CFBundleVersion", "1")) + 1)
        (candidate / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(candidate)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(candidate)], check=True)
        app.rename(backup)
        try:
            candidate.rename(app)
            subprocess.run(register, check=True)
        except (OSError, subprocess.SubprocessError):
            if app.exists():
                app.rename(candidate)
            backup.rename(app)
            subprocess.run(register, check=False, capture_output=True)
            raise
    return {**result, "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description="给已安装的 Hermes 学习通知 App 更新图标")
    parser.add_argument("--app", type=Path, default=Path.home() / "Applications/Hermes 学习.app")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("请在运行 Codex Desktop 的 Mac 上执行")
    icon = Path(__file__).resolve().parent.parent / "assets/hermes-learning-icon.icns"
    try:
        result = update_icon(args.app.expanduser(), icon, dry_run=args.dry_run)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
