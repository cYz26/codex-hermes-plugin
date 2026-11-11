#!/usr/bin/env python3
"""Migrate the cached sender identity, preserve legacy receipts, and test delivery."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.macos_bundle import APP_ID, LEGACY_APP_ID, NotificationAuthorizationError, bundle_identifier, install_notification_app, legacy_notifier_app
from diagnose_macos_icon import diagnose, receiver_config, sender_app
from repair_and_test_macos_icon import save

NOTIFICATION_SETTINGS_URL = "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=" + APP_ID


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_with_authorization_recovery(root: Path, app: Path, report: dict, destination: Path, recover: bool) -> dict:
    for attempt in (0, 1):
        try:
            installation = install_notification_app(root, app)
        except NotificationAuthorizationError as exc:
            report["authorization"] = exc.result
            report["pending_app"] = str(exc.app)
            report.setdefault("authorization_attempts", []).append({"at": timestamp(), "result": exc.result})
            if not recover or attempt != 0 or exc.result.get("bundle_identifier") != APP_ID or exc.result.get("authorization_status") != 1:
                raise
            report["state"] = "waiting_for_authorization"
            report["settings_url"] = NOTIFICATION_SETTINGS_URL
            save(destination, report)
            print("macOS 仍拒绝新版 Hermes 学习通知。正在打开它对应的通知设置。", flush=True)
            subprocess.run(["/usr/bin/open", NOTIFICATION_SETTINGS_URL], check=True)
            try:
                input("请开启「允许通知」，然后返回此终端按回车继续：")
            except EOFError:
                raise NotificationAuthorizationError(exc.app, {
                    **exc.result, "error": "未收到终端确认；请双击恢复脚本并在开启通知后按回车继续"}) from exc
            report["state"] = "checking_authorization"
        else:
            report["authorization"] = installation["authorization"]
            report.setdefault("authorization_attempts", []).append({"at": timestamp(), "result": installation["authorization"]})
            report.pop("pending_app", None)
            return installation


def post_test(app: Path, output: Path) -> dict:
    record = output / "notification-refresh-test.json"
    if record.exists():
        value = json.loads(record.read_text())
        if value.get("transport", LEGACY_APP_ID) != APP_ID:
            record.rename(output / ("legacy-refresh-test-" + value["marker"] + ".json"))
            return post_test(app, output)
        if Path(value["receipt"] + ".sent").is_file():
            value["state"] = "sent"
        return {**value, "reused": True}
    marker = uuid.uuid4().hex[:6].upper()
    receipt = output / f"icon-test-C-{marker}.json"
    save(receipt, {"summary": f"【新发送方图标测试 {marker}】请核对 Hermes 学习图标。",
                   "scope": "通知图标测试", "text": "这是迁移通知发送方标识后的显示测试，不是学习成果。"})
    value = {"marker": marker, "receipt": str(receipt), "state": "dispatching", "visibility": "not_inferred", "transport": bundle_identifier(app)}
    save(record, value)
    try:
        result = subprocess.run([str(app / "Contents/MacOS/HermesLearningNotifier"), "--post", str(receipt)],
                                capture_output=True, text=True, timeout=15)
        if Path(str(receipt) + ".sent").is_file():
            value["state"] = "sent"
        else:
            value["state"] = "rejected" if result.returncode == 1 else "unknown"
            value["error"] = result.stderr.strip() or "没有系统接受标记，不自动重发"
        value["exit_code"] = result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        value["state"], value["error"] = "unknown", str(exc)
    save(record, value)
    return {**value, "reused": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="安装带图标的新通知发送方、保留历史回执并测试显示")
    parser.add_argument("--recover-authorization", action="store_true", help="授权被拒时打开新版通知设置，按回车后重新检查一次")
    arguments = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("请在运行 Codex Desktop 的 Mac 上执行")
    output = Path(__file__).resolve().parent.parent / "report"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output / "notification-refresh-report.json"
    report = {"visibility": "not_inferred", "state": "preparing", "started_at": timestamp()}
    try:
        app, source = sender_app(receiver_config())
        report["app"], report["sender_source"] = str(app), source
        report["before"] = diagnose(app, output) if app.exists() else {
            "app_present": False, "legacy_identifier": bundle_identifier(legacy_notifier_app(app))}
        print("正在安装新的通知发送方；首次迁移时，请在 macOS 提示中允许「Hermes 学习」发送通知。", flush=True)
        report["installation"] = install_with_authorization_recovery(
            Path(__file__).resolve().parent.parent, app, report, destination, arguments.recover_authorization)
        report["state"] = "installed"
        report["after"] = diagnose(app, output)
        if report["after"]["bundle_identifier"] != APP_ID or not report["after"].get("signature_verified"):
            raise ValueError("新通知发送方验证未通过，未发送测试通知")
        report["test"] = post_test(app, output)
        report["state"] = report["test"]["state"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, NotificationAuthorizationError) as exc:
        report["state"] = "failed"
        report["error"] = str(exc)
        if isinstance(exc, NotificationAuthorizationError):
            report["authorization"] = exc.result
            report["pending_app"] = str(exc.app)
            report["active_sender"] = {"app": str(app), "bundle_identifier": bundle_identifier(app)}
            report["settings_url"] = NOTIFICATION_SETTINGS_URL
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            report["command_error"] = exc.stderr.strip()

    except KeyboardInterrupt:
        report["state"], report["error"] = "cancelled", "已取消本次安装"
    report["finished_at"] = timestamp()
    save(destination, report)
    print(destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report.get("error") and report.get("test", {}).get("state") == "sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
