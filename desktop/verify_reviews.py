#!/usr/bin/env python3
"""Check installed review assets and a disposable native workflow; optionally open a display fixture."""
import argparse
import hashlib
import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review import RemoteReviews


def local_checks(root, config):
    state = Path(config["state"]).expanduser()
    app = Path(config["notifier"]).expanduser().parents[2]
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assets = all((state / name).is_file() and (state / name).read_bytes() == (root / "desktop" / name).read_bytes()
                 for name in ("review.py", "review.html"))
    return {"assets_match_package": assets, "sender_matches": info.get("CFBundleIdentifier") == "com.cy.hermes-learning-receipts",
            "review_protocol": info.get("HermesReviewProtocolVersion") == 1,
            "source_matches_package": info.get("HermesReviewSourceSHA256") == hashlib.sha256(
                (root / "macos/HermesLearningNotifier.swift").read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description="Verify the Mac approval entry without approving live proposals")
    parser.add_argument("--config", type=Path, default=Path.home() / "Library/Application Support/HermesLearningSSH/config.json")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--show", action="store_true", help="Open a clearly marked local display receipt; does not post a notification")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run native viewer acceptance on the Mac that runs Codex Desktop")
    root = Path(__file__).resolve().parents[1]
    config = json.loads(args.config.read_text())
    config.setdefault("remote_python", "/usr/bin/python3")
    state = Path(config["state"]).expanduser()
    report = {"state": "checking", "native_ui_acceptance": "requires_user_observation"}
    try:
        report["local"] = local_checks(root, config)
        if not all(report["local"].values()):
            raise RuntimeError("安装的审批资源或通知 App 与验收包不匹配，请先运行安装入口")
        app = Path(config["notifier"]).expanduser().parents[2]
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True, capture_output=True)
        report["signature"] = "verified"
        report["native_contracts"] = RemoteReviews(config, args.cwd).self_test()
        if report["native_contracts"]["state"] != "passed":
            raise RuntimeError("开发机原生审批自检未通过")
        if args.show:
            marker = "approval-ui-" + str(time.time_ns())
            receipt = state / (marker + ".json")
            receipt.write_text(json.dumps({"summary": "Hermes 审批入口显示验收", "scope": args.cwd,
                "text": "显示测试：" + marker + "\n\n这份回执是本地显示样例，不会审批或修改真实记忆。\n"
                        "请点击「查看当前待审」，确认浏览器能显示该项目的提案差异和操作按钮。"}, ensure_ascii=False))
            receipt.chmod(0o600)
            subprocess.run(["/usr/bin/open", "-n", "-a", str(app), "--args", "--show-receipt", str(receipt)], check=True)
            report["display_fixture"] = {"marker": marker, "opened": True, "notification_posted": False}
        report["state"] = "checks_passed"
    except Exception as error:
        report.update(state="failed", error_type=type(error).__name__, error=str(error))
    path = state / "review-verification-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    path.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["state"] == "checks_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
