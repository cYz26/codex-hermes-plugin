"""Build, authorize and replace the notification App while preserving legacy receipts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "Hermes 学习"
APP_ID = "com.cy.hermes-learning-receipts"
LEGACY_APP_ID = "com.cy.hermes-learning-notifier"
APP_EXECUTABLE = "HermesLearningNotifier"
REVIEW_ASSETS = ("review.py", "review.html")
REGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


def notifier_app() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def legacy_notifier_app(app: Path) -> Path:
    return app.parent / ".hermes-learning-legacy" / app.name


def pending_notifier_app(app: Path) -> Path:
    return app.parent / ".hermes-learning-pending" / app.name


class NotificationAuthorizationError(RuntimeError):
    def __init__(self, app: Path, result: dict):
        self.app, self.result = app, result
        reason = str(result.get("error") or "macOS 未允许新发送方发送通知")
        super().__init__(reason + "；请在「系统设置 → 通知 → Hermes 学习」允许通知后重新运行")


def authorize_notification_app(app: Path, result_path: Path, timeout: float = 60) -> dict:
    subprocess.run([REGISTER, "-f", str(app)], check=True, capture_output=True, text=True)
    subprocess.run(["/usr/bin/open", "-n", "-a", str(app), "--args", "--authorize-result", str(result_path)],
                   check=True, capture_output=True, text=True)
    deadline = time.monotonic() + timeout
    while not result_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not result_path.is_file():
        raise NotificationAuthorizationError(app, {"allowed": False, "error": "没有收到授权结果，未替换原 App"})
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError) as exc:
        raise NotificationAuthorizationError(app, {"allowed": False, "error": "无法读取授权结果：" + str(exc)}) from exc
    if not isinstance(result, dict):
        raise NotificationAuthorizationError(app, {"allowed": False, "error": "授权结果不是对象，未替换原 App"})
    reported_path = result.get("bundle_path")
    matches_app = isinstance(reported_path, str) and Path(reported_path).resolve() == app.resolve()
    if (result.get("bundle_identifier") != APP_ID or not matches_app or result.get("allowed") is not True
            or result.get("authorization_status") not in (2, 3) or result.get("error") not in (None, "")):
        raise NotificationAuthorizationError(app, result)
    return result


def bundle_identifier(app: Path) -> str | None:
    try:
        with (app / "Contents/Info.plist").open("rb") as info:
            return plistlib.load(info).get("CFBundleIdentifier")
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def install_lock(path: Path):
    import fcntl
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_macos_icon(png: Path, output: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix=".hermes-iconset-", dir=output.parent) as temporary:
        iconset = Path(temporary) / "HermesLearning.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                size = points * scale
                suffix = "@2x" if scale == 2 else ""
                destination = iconset / f"icon_{points}x{points}{suffix}.png"
                subprocess.run(["/usr/bin/sips", "-z", str(size), str(size), str(png), "--out", str(destination)],
                               check=True, capture_output=True, timeout=15)
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(output)],
                       check=True, capture_output=True, timeout=15)
    return output


def normalize_bundle_permissions(app: Path) -> dict:
    info_path = app / "Contents/Info.plist"
    for path in (app, app / "Contents", info_path):
        if path.is_symlink():
            raise ValueError("通知 App 的目标路径不能是符号链接：" + str(path))
    info = plistlib.loads(info_path.read_bytes())
    if info.get("CFBundleIdentifier") not in (APP_ID, LEGACY_APP_ID):
        raise ValueError("不是受管 Hermes 通知 App")
    executable = info.get("CFBundleExecutable")
    icon = info.get("CFBundleIconFile")
    if executable != "HermesLearningNotifier" or not isinstance(icon, str) or Path(icon).name != icon:
        raise ValueError("通知 App 的可执行文件或图标声明不符合预期")
    directories = [app, app / "Contents", app / "Contents/MacOS", app / "Contents/Resources",
                   app / "Contents/_CodeSignature"]
    icon_path = app / "Contents/Resources" / icon
    if not icon_path.exists() and not icon_path.suffix:
        icon_path = icon_path.with_suffix(".icns")
    files = [(info_path, 0o644), (app / "Contents/MacOS" / executable, 0o755),
             (icon_path, 0o644),
             (app / "Contents/_CodeSignature/CodeResources", 0o644)]
    files.extend((app / "Contents/Resources" / name, 0o644) for name in REVIEW_ASSETS)
    plan = []
    for path, mode in [(path, 0o755) for path in directories] + files:
        if path.is_symlink():
            raise ValueError("通知 App 的目标路径不能是符号链接：" + str(path))
        if path.exists():
            old = path.stat().st_mode & 0o777
            if old != mode:
                plan.append((path, old, mode))
    applied = []
    try:
        for path, old, mode in plan:
            path.chmod(mode)
            applied.append((path, old))
    except OSError:
        for path, old in reversed(applied):
            path.chmod(old)
        raise
    return {str(path.relative_to(app)): {"before": oct(old), "after": oct(mode)} for path, old, mode in plan}


def install_notification_app(root: Path, app: Path | None = None) -> dict:
    """Authorize a new sender before replacing the active App; keep legacy receipts."""
    app = notifier_app() if app is None else app
    created_parent = not app.parent.exists()
    app.parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        app.parent.chmod(0o755)
    legacy = legacy_notifier_app(app)
    pending = pending_notifier_app(app)
    with install_lock(app.parent / ".hermes-learning-notifier.lock"), \
            tempfile.TemporaryDirectory(prefix=".hermes-learning-", dir=app.parent) as temporary:
        staging = Path(temporary)
        staging.chmod(0o755)
        installed_id = bundle_identifier(app)
        if app.exists() and installed_id not in (APP_ID, LEGACY_APP_ID):
            raise FileExistsError(str(app))
        migrating = installed_id == LEGACY_APP_ID
        if legacy.exists() and (migrating or bundle_identifier(legacy) != LEGACY_APP_ID):
            raise FileExistsError(str(legacy))
        candidate = staging / app.name
        contents = candidate / "Contents"
        binary = contents / "MacOS" / APP_EXECUTABLE
        binary.parent.mkdir(parents=True)
        resources = contents / "Resources"
        resources.mkdir()
        icon = build_macos_icon(root / "assets/hermes-learning-icon.png", staging / "HermesLearning.icns")
        icon_name = "hermes-learning-icon-" + hashlib.sha256(icon.read_bytes()).hexdigest()[:12] + ".icns"
        shutil.copy2(icon, resources / icon_name)
        for name in REVIEW_ASSETS:
            shutil.copy2(root / "desktop" / name, resources / name)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": APP_ID, "CFBundleExecutable": APP_EXECUTABLE,
            "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
            "CFBundlePackageType": "APPL", "CFBundleVersion": "7", "LSMinimumSystemVersion": "12.0",
            "HermesReviewProtocolVersion": 1,
            "HermesReviewPython": sys.executable,
            "HermesReviewSourceSHA256": hashlib.sha256((root / "macos/HermesLearningNotifier.swift").read_bytes()).hexdigest(),
            "CFBundleIconFile": icon_name, "LSUIElement": True,
        }))
        target = f"{platform.machine()}-apple-macosx12.0"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-target", target, "-O",
                        str(root / "macos/HermesLearningNotifier.swift"), "-o", str(binary)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", APP_ID, str(candidate)], check=True)
        normalize_bundle_permissions(candidate)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(candidate)], check=True)
        # Authorization must run through LaunchServices from a registered, stable path.
        if pending.exists():
            if bundle_identifier(pending) != APP_ID:
                raise FileExistsError(str(pending))
            subprocess.run([str(binary), "--prepare-upgrade", str(pending)], check=True, timeout=15)
            previous_pending = staging / "previous-pending.app"
            pending.rename(previous_pending)
            try:
                candidate.rename(pending)
            except (OSError, KeyboardInterrupt):
                previous_pending.rename(pending)
                raise
        else:
            pending.parent.mkdir(exist_ok=True)
            pending.parent.chmod(0o755)
            candidate.rename(pending)
        binary = pending / "Contents/MacOS" / APP_EXECUTABLE
        authorization = authorize_notification_app(pending, staging / "authorization-result.json")
        subprocess.run([str(binary), "--prepare-upgrade", str(app)], check=True, timeout=15)
        backup = legacy if migrating else staging / "previous.app"
        try:
            if app.exists():
                created_backup_parent = not backup.parent.exists()
                backup.parent.mkdir(exist_ok=True)
                if created_backup_parent:
                    backup.parent.chmod(0o755)
                app.rename(backup)
            pending.rename(app)
            applications = [str(legacy), str(app)] if legacy.exists() else [str(app)]
            subprocess.run([REGISTER, "-f", *applications], check=True)
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
            if app.exists() and not pending.exists():
                app.rename(pending)
            if backup.exists() and not app.exists():
                backup.rename(app)
            if app.exists():
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([REGISTER, "-f", str(app)], check=True, capture_output=True)
            raise
    return {"app": str(app), "transport": APP_ID, "migrated": migrating, "authorization": authorization,
            "legacy_app": str(legacy) if legacy.exists() else None, "visibility": "not_inferred"}
