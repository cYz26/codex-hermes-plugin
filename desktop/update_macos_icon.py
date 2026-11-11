#!/usr/bin/env python3
"""Compatibility entry point for the complete icon sender migration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.macos_bundle import (
    APP_ID, LEGACY_APP_ID, bundle_identifier,
    install_notification_app, legacy_notifier_app, notifier_app,
)


def update_icon(app: Path, *, dry_run: bool = False) -> dict:
    # A resource-only update cannot migrate the notification identity safely.
    if dry_run:
        return {"app": str(app), "transport": APP_ID,
                "migrating": bundle_identifier(app) == LEGACY_APP_ID,
                "legacy_app": str(legacy_notifier_app(app)), "dry_run": True}
    return install_notification_app(Path(__file__).resolve().parent.parent, app)


def main() -> int:
    parser = argparse.ArgumentParser(description="安装带图标的新通知发送方并保留历史回执")
    parser.add_argument("--app", type=Path, default=notifier_app())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("请在运行 Codex Desktop 的 Mac 上执行")
    result = update_icon(args.app.expanduser(), dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
