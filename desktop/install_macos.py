#!/usr/bin/env python3
"""Install the reviewed SSH receiver on the user's Mac, without changing Codex."""
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

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--bridge", required=True)
parser.add_argument("--settings", required=True)
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
if sys.platform != "darwin":
    parser.error("Run this installer on the Mac that runs Codex Desktop")
root = Path(__file__).resolve().parent.parent
state = Path.home() / "Library/Application Support/HermesLearningSSH"
app = Path.home() / "Applications/Hermes 学习.app"
binary = app / "Contents/MacOS/HermesLearningNotifier"
label = "com.cy.hermes-learning-ssh"
plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
config = {"host": args.host, "bridge": args.bridge, "settings": args.settings,
          "state": str(state), "notifier": str(binary)}
if args.dry_run:
    print(json.dumps({"config": config, "app": str(app), "launch_agent": str(plist),
                      "chat_ui": "unavailable", "codex_settings_changed": False}, ensure_ascii=False, indent=2))
    raise SystemExit(0)
os.umask(0o077)
state.mkdir(parents=True, exist_ok=True)
app.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix=".hermes-learning-build-", dir=app.parent) as temporary:
    candidate = Path(temporary) / app.name
    candidate_binary = candidate / "Contents/MacOS/HermesLearningNotifier"
    candidate_binary.parent.mkdir(parents=True)
    subprocess.run(["xcrun", "swiftc", "-O", str(root / "macos/HermesLearningNotifier.swift"), "-o", str(candidate_binary)], check=True)
    (candidate / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.cy.hermes-learning-notifier",
        "CFBundleName": "Hermes 学习", "CFBundleExecutable": "HermesLearningNotifier", "CFBundlePackageType": "APPL", "LSUIElement": True}))
    subprocess.run(["codesign", "--force", "--sign", "-", str(candidate)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(candidate)], check=True)
    previous = app.parent / (".HermesLearning.previous." + str(time.time_ns()) + ".app")
    if app.exists():
        app.rename(previous)
    try:
        candidate.rename(app)
    except OSError:
        if previous.exists() and not app.exists():
            previous.rename(app)
        raise
shutil.copy2(root / "desktop/receive.py", state / "receive.py")
(state / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
plist.parent.mkdir(parents=True, exist_ok=True)
if plist.exists():
    shutil.copy2(plist, state / "previous-launch-agent.plist")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False, capture_output=True)
plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [sys.executable, str(state / "receive.py"),
    "--config", str(state / "config.json")], "RunAtLoad": True, "KeepAlive": True,
    "ThrottleInterval": 10, "StandardErrorPath": str(state / "receiver-error.log")}))
subprocess.run(["open", "-gnj", "-a", str(app), "--args", "--authorize"], check=True)
subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
print(json.dumps({"installed": True, "visibility": "requires_user_verification", "config": str(state / "config.json")}, ensure_ascii=False))
