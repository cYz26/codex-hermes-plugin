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
APP_ID = "com.cy.hermes-learning-receipts-v2"
PREVIOUS_APP_ID = "com.cy.hermes-learning-receipts"
LEGACY_APP_ID = "com.cy.hermes-learning-notifier"
PROJECT_LEGACY_APP_IDS = (PREVIOUS_APP_ID, LEGACY_APP_ID)
GLOBAL_APP_NAME = "Hermes 全局共享"
GLOBAL_APP_ID = "com.cy.hermes-learning-global-receipts-v2"
LEGACY_GLOBAL_APP_ID = "com.cy.hermes-learning-global-receipts"
APP_EXECUTABLE = "HermesLearningNotifier"
APP_BUILD = "12"
REVIEW_ASSETS = ("review.py", "review.html")
APPROVAL_ICON = "hermes-approval-indicator.png"
REGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


def notifier_app() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def global_notifier_app(project_app: Path | None = None) -> Path:
    return (project_app or notifier_app()).with_name(f"{GLOBAL_APP_NAME}.app")


def legacy_notifier_app(app: Path, identifier: str | None = None) -> Path:
    archive = app.parent / ".hermes-learning-legacy"
    if identifier == PREVIOUS_APP_ID:
        archive = archive / PREVIOUS_APP_ID
    return archive / app.name


def installed_legacy_project_notifier(app: Path) -> Path | None:
    for identifier in PROJECT_LEGACY_APP_IDS:
        legacy = legacy_notifier_app(app, identifier)
        if bundle_identifier(legacy) == identifier:
            return legacy
    return None


def pending_notifier_app(app: Path) -> Path:
    return app.parent / ".hermes-learning-pending" / app.name


class NotificationAuthorizationError(RuntimeError):
    def __init__(self, app: Path, result: dict):
        self.app, self.result = app, result
        reason = str(result.get("error") or "macOS 未允许新发送方发送通知")
        super().__init__(reason + f"。请在「系统设置 → 通知 → {app.stem}」允许通知后重新运行")


def authorize_notification_app(app: Path, result_path: Path, timeout: float = 60) -> dict:
    identifier = bundle_identifier(app)
    if identifier not in (APP_ID, GLOBAL_APP_ID):
        raise ValueError("不是受管 Hermes 通知 App")
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
    if (result.get("bundle_identifier") != identifier or not matches_app or result.get("allowed") is not True
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
    if info.get("CFBundleIdentifier") not in (APP_ID, GLOBAL_APP_ID, LEGACY_GLOBAL_APP_ID, *PROJECT_LEGACY_APP_IDS):
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
    files.extend((app / "Contents/Resources" / name, 0o644) for name in (*REVIEW_ASSETS, APPROVAL_ICON))
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


def install_notification_app(root: Path, app: Path | None = None, *, global_sender: bool = False) -> dict:
    """Authorize a new sender before replacing the active App; keep legacy receipts."""
    app = app or (global_notifier_app() if global_sender else notifier_app())
    identifier = GLOBAL_APP_ID if global_sender else APP_ID
    legacy_ids = (LEGACY_GLOBAL_APP_ID,) if global_sender else PROJECT_LEGACY_APP_IDS
    name = GLOBAL_APP_NAME if global_sender else APP_NAME
    icon_stem = "hermes-global-notification" if global_sender else "hermes-project-notification"
    created_parent = not app.parent.exists()
    app.parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        app.parent.chmod(0o755)
    archives = {legacy_id: legacy_notifier_app(app, legacy_id) for legacy_id in legacy_ids}
    pending = pending_notifier_app(app)
    with install_lock(app.parent / ".hermes-learning-notifier.lock"), \
            tempfile.TemporaryDirectory(prefix=".hermes-learning-", dir=app.parent) as temporary:
        staging = Path(temporary)
        staging.chmod(0o755)
        installed_id = bundle_identifier(app)
        if app.is_symlink() or (app.exists() and installed_id not in (identifier, *legacy_ids)):
            raise FileExistsError(str(app))
        for legacy_id, archive in archives.items():
            if archive.is_symlink() or (archive.exists() and
                    (installed_id == legacy_id or bundle_identifier(archive) != legacy_id)):
                raise FileExistsError(str(archive))
        legacy = archives.get(installed_id)
        candidate = staging / app.name
        contents = candidate / "Contents"
        binary = contents / "MacOS" / APP_EXECUTABLE
        binary.parent.mkdir(parents=True)
        resources = contents / "Resources"
        resources.mkdir()
        icon = build_macos_icon(root / "assets" / (icon_stem + ".png"), staging / "HermesLearning.icns")
        icon_name = icon_stem + "-" + hashlib.sha256(icon.read_bytes()).hexdigest()[:12] + ".icns"
        shutil.copy2(icon, resources / icon_name)
        shutil.copy2(root / "assets" / APPROVAL_ICON, resources / APPROVAL_ICON)
        for resource in REVIEW_ASSETS:
            shutil.copy2(root / "desktop" / resource, resources / resource)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": APP_EXECUTABLE,
            "CFBundleName": name, "CFBundleDisplayName": name,
            "CFBundlePackageType": "APPL", "CFBundleVersion": APP_BUILD, "LSMinimumSystemVersion": "12.0",
            "HermesReviewProtocolVersion": 1,
            "HermesReviewPython": sys.executable,
            "HermesReviewSourceSHA256": hashlib.sha256((root / "macos/HermesLearningNotifier.swift").read_bytes()).hexdigest(),
            "CFBundleIconFile": icon_name, "LSUIElement": True,
        }))
        target = f"{platform.machine()}-apple-macosx12.0"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-target", target, "-O",
                        str(root / "macos/HermesLearningNotifier.swift"), "-o", str(binary)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", identifier, str(candidate)], check=True)
        normalize_bundle_permissions(candidate)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(candidate)], check=True)
        # Authorization must run through LaunchServices from a registered, stable path.
        if pending.exists():
            if pending.is_symlink() or bundle_identifier(pending) not in (identifier, *legacy_ids):
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
        if app.exists():
            subprocess.run([str(binary), "--prepare-upgrade", str(app)], check=True, timeout=15)
        backup = legacy if legacy is not None else staging / "previous.app"
        try:
            if app.exists():
                if legacy is not None:
                    for directory in (legacy_notifier_app(app).parent, legacy.parent):
                        if not directory.exists():
                            directory.mkdir()
                            directory.chmod(0o755)
                app.rename(backup)
            pending.rename(app)
            archived = [str(archive) for archive in archives.values() if archive.exists()]
            subprocess.run([REGISTER, "-f", *archived, str(app)], check=True)
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
            if app.exists() and not pending.exists():
                app.rename(pending)
            if backup.exists() and not app.exists():
                backup.rename(app)
            if app.exists():
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([REGISTER, "-f", str(app)], check=True, capture_output=True)
            raise
    return {"app": str(app), "transport": identifier, "migrated": legacy is not None, "authorization": authorization,
            "legacy_app": archived[0] if archived else None, "visibility": "not_inferred"}


def install_notification_apps(root: Path, app: Path | None = None) -> dict:
    global_sender = install_notification_app(root, global_notifier_app(app), global_sender=True)
    return {**install_notification_app(root, app), "global_sender": global_sender}
