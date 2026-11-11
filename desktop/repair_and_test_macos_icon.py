#!/usr/bin/env python3
"""Install the notification sender and compare the two notification launch methods."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from diagnose_macos_icon import diagnose, receiver_config, sender_app
from update_macos_icon import update_icon
from hermes_codex_learning.macos_bundle import APP_ID, LEGACY_APP_ID, NotificationAuthorizationError, bundle_identifier


def save(path: Path, value: dict) -> None:
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".icon-test-", delete=False) as temporary:
            pending = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(pending, path)
        pending = None
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def post_tests(app: Path, output: Path, icon_hash: str, *, new_test: bool = False) -> dict:
    plan_path = output / "notification-icon-test-plan.json"
    transport = bundle_identifier(app)
    if transport not in (APP_ID, LEGACY_APP_ID):
        raise ValueError("测试目标不是受管 Hermes 通知 App")
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if new_test or plan.get("transport") != transport:
            plan_path.rename(output / ("previous-icon-test-plan-" + uuid.uuid4().hex + ".json"))
        else:
            for test in plan["tests"]:
                if Path(test["receipt"] + ".sent").is_file():
                    test["state"] = "sent"
            save(plan_path, plan)
            return {**plan, "reused": True, "matches_current_icon": plan["icon_sha256"] == icon_hash}

    marker = uuid.uuid4().hex[:6].upper()
    tests = []
    for label, title in (("A", "现有投递方式"), ("B", "系统启动方式")):
        receipt = output / f"icon-test-{marker}-{label}.json"
        save(receipt, {"summary": f"【图标测试 {label} {marker}】{title}，请核对左侧图标。",
                       "scope": "通知图标测试", "text": f"这是通知显示测试，不是学习成果。\n方式 {label}：{title}\n测试标识：{marker}"})
        tests.append({"label": label, "receipt": str(receipt), "state": "prepared", "transport": transport})
    plan = {"marker": marker, "transport": transport, "icon_sha256": icon_hash, "tests": tests, "visibility": "not_inferred"}
    save(plan_path, plan)

    binary = app / "Contents/MacOS/HermesLearningNotifier"
    for test in tests:
        path = Path(test["receipt"])
        command = ([str(binary), "--post", str(path)] if test["label"] == "A" else
                   ["/usr/bin/open", "-g", "-n", "-a", str(app), "--args", "--post", str(path)])
        test["state"] = "dispatching"
        save(plan_path, plan)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            test["exit_code"] = result.returncode
            deadline = time.monotonic() + (15 if result.returncode == 0 else 0)
            while not Path(str(path) + ".sent").is_file() and time.monotonic() < deadline:
                time.sleep(0.2)
            if Path(str(path) + ".sent").is_file():
                test["state"] = "sent"
            else:
                test["state"] = "rejected" if test["label"] == "A" and result.returncode == 1 else "unknown"
                test["error"] = result.stderr.strip() or "没有收到系统接受标记，不会自动重发"
        except (OSError, subprocess.TimeoutExpired) as exc:
            test["state"], test["error"] = "unknown", str(exc)
        save(plan_path, plan)
    return {**plan, "reused": False, "matches_current_icon": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 Hermes 学习图标并发送两种启动方式的测试通知")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--new-test", action="store_true", help="明确请求新一轮测试；默认读取既有测试回执")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("请在运行 Codex Desktop 的 Mac 上执行")
    root = Path(__file__).resolve().parent.parent
    output = root / "report"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"macos_version": platform.mac_ver()[0], "visibility": "not_inferred"}
    with (output / ".repair.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有图标修复或测试正在执行，请等待该窗口完成。", file=sys.stderr)
            return 1
        try:
            app, source = sender_app(receiver_config(), args.app)
            report["app"], report["sender_source"] = str(app), source
            report["preview"] = update_icon(app, dry_run=True)
            print(json.dumps(report["preview"], ensure_ascii=False, indent=2), flush=True)
            print("正在生成图标并安装通知 App…", flush=True)
            report["update"] = update_icon(app)
            print("正在核对新图标和签名…", flush=True)
            report["diagnostic"] = diagnose(app, output)
            diagnostic = report["diagnostic"]
            if not diagnostic.get("signature_verified") or not diagnostic.get("macos", {}).get("native_icon_decoded"):
                raise ValueError("图标更新后验证未通过，请查看报告；未发送测试通知")
            print("正在发送测试 A（现有方式）和 B（系统启动）…", flush=True)
            report["notification_tests"] = post_tests(app, output, diagnostic["icon_sha256"], new_test=args.new_test)
        except NotificationAuthorizationError as exc:
            report["error"] = str(exc)
            report["authorization"] = exc.result
            report["pending_app"] = str(exc.app)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            report["error"] = str(exc)
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                report["command_error"] = exc.stderr.strip()
        destination = output / "notification-icon-repair-report.json"
        save(destination, report)
        print(destination)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("请比较两条新通知，记录 A 和 B 的图标是否出现；系统接受回执不能证明显示效果。")
        tests = report.get("notification_tests", {})
        results = tests.get("tests", [])
        accepted = tests.get("matches_current_icon") and len(results) == 2 and all(test["state"] == "sent" for test in results)
        return 0 if not report.get("error") and accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
