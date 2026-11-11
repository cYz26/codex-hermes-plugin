#!/usr/bin/env python3
"""Read the actual SSH notifier bundle and macOS icon lookup; never posts a notice."""
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.macos_bundle import APP_ID, LEGACY_APP_ID


def receiver_config() -> Path:
    agent = Path.home() / "Library/LaunchAgents/com.cy.hermes-learning-ssh.plist"
    if agent.is_file():
        arguments = plistlib.loads(agent.read_bytes()).get("ProgramArguments", [])
        if "--config" in arguments:
            index = arguments.index("--config")
            if index + 1 < len(arguments):
                return Path(arguments[index + 1]).expanduser()
    return Path.home() / "Library/Application Support/HermesLearningSSH/config.json"


def sender_app(receiver: Path, override: Path | None = None) -> tuple[Path, str]:
    if override is not None:
        return override.expanduser().resolve(), "explicit_app"
    if receiver.is_file():
        notifier = Path(json.loads(receiver.read_text())["notifier"]).expanduser().resolve()
        for parent in notifier.parents:
            if parent.suffix == ".app":
                return parent, str(notifier)
        raise ValueError("接收器配置的 notifier 不在 .app 包内")
    return (Path.home() / "Applications/Hermes 学习.app").resolve(), "default_app"


def icns_entries(data: bytes) -> list[dict]:
    if len(data) < 8 or data[:4] != b"icns" or struct.unpack(">I", data[4:8])[0] != len(data):
        raise ValueError("Invalid ICNS header")
    result, offset = [], 8
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated ICNS entry")
        kind, length = struct.unpack(">4sI", data[offset:offset + 8])
        if length < 8 or offset + length > len(data):
            raise ValueError("Invalid ICNS entry length")
        result.append({"type": kind.decode("ascii"), "bytes": length})
        offset += length
    return result


def diagnose(app: Path, output: Path) -> dict:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") not in (APP_ID, LEGACY_APP_ID):
        raise ValueError("接收器目标不是 Hermes 学习通知 App")
    report = {"app": str(app), "bundle_identifier": info["CFBundleIdentifier"],
              "bundle_version": info.get("CFBundleVersion"),
              "icon_file": info.get("CFBundleIconFile"), "icon_name": info.get("CFBundleIconName"),
              "visibility": "not_inferred"}
    icon = None
    name = info.get("CFBundleIconFile")
    if isinstance(name, str) and name and Path(name).name == name:
        icon = app / "Contents/Resources" / name
        if not icon.is_file() and not icon.suffix:
            icon = icon.with_suffix(".icns")
    report["icon_exists"] = icon is not None and icon.is_file()
    paths = {"app": app, "contents": app / "Contents", "resources": app / "Contents/Resources",
             "info": app / "Contents/Info.plist", "binary": app / "Contents/MacOS/HermesLearningNotifier",
             "apps_parent": app.parent}
    if report["icon_exists"]:
        paths["icon"] = icon
    report["permissions"] = {name: oct(path.stat().st_mode & 0o777) for name, path in paths.items() if path.exists()}
    if report["icon_exists"]:
        data = icon.read_bytes()
        report["icon_sha256"] = hashlib.sha256(data).hexdigest()
        try:
            report["icns_entries"] = icns_entries(data)
        except ValueError as exc:
            report["icns_error"] = str(exc)
    try:
        signed = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)],
                                capture_output=True, text=True, timeout=15)
        report["signature_verified"] = signed.returncode == 0
        if signed.returncode:
            report["signature_error"] = signed.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["signature_error"] = str(exc)
    try:
        signing = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=2", str(app)],
                                 capture_output=True, text=True, timeout=15)
        lines = (signing.stdout + "\n" + signing.stderr).splitlines()
        identifier = next((line.partition("=")[2] for line in lines if line.startswith("Identifier=")), None)
        if signing.returncode == 0 and identifier:
            report["signing_identifier"] = identifier
            report["signing_identifier_matches_bundle"] = identifier == info["CFBundleIdentifier"]
        else:
            report["signing_identifier_error"] = "无法读取签名中的应用标识"
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["signing_identifier_error"] = str(exc)
    helper = Path(__file__).resolve().parent.parent / "macos/DiagnoseNotifierIcon.swift"
    try:
        native = subprocess.run(["/usr/bin/xcrun", "swift", str(helper), str(app),
                                 str(icon) if report["icon_exists"] else "", str(output)],
                                capture_output=True, text=True, timeout=45)
        if native.returncode:
            report["native_error"] = native.stderr.strip() or f"Native check exited with {native.returncode}"
        else:
            report["macos"] = json.loads(native.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        report["native_error"] = str(exc)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查 Hermes 通知 App 的图标和 macOS 注册信息")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--receiver-config", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent.parent / "report")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("请在运行 Codex Desktop 的 Mac 上执行")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        config = args.receiver_config.expanduser() if args.receiver_config else receiver_config()
        app, source = sender_app(config, args.app)
        report = {"receiver_config": str(config), "sender_source": source, **diagnose(app, output)}
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as exc:
        report = {"error": str(exc), "visibility": "not_inferred"}
    destination = output / "notification-icon-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    destination.chmod(0o600)
    print(destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get("error") or report.get("native_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
