import importlib.util
import json
import os
import plistlib
import runpy
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.macos_bundle import (APP_ID, LEGACY_APP_ID, APP_EXECUTABLE,
    NotificationAuthorizationError, authorize_notification_app, bundle_identifier, install_notification_app,
    legacy_notifier_app, normalize_bundle_permissions, pending_notifier_app)


def desktop_module(name):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / "desktop" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(root / "desktop"), *sys.path]):
        spec.loader.exec_module(module)
    return module


class MacOSBundleTests(unittest.TestCase):
    @staticmethod
    def native(argv, **kwargs):
        if Path(argv[0]).name == "xcrun":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"synthetic notifier")
        elif Path(argv[0]).name in {"sips", "iconutil"}:
            Path(argv[-1]).write_bytes(b"synthetic icon")
        elif Path(argv[0]).name == "codesign" and "--force" in argv:
            directory = Path(argv[-1]) / "Contents/_CodeSignature"
            directory.mkdir(exist_ok=True)
            (directory / "CodeResources").write_bytes(b"synthetic resource seal")
        elif Path(argv[0]).name == "open" and "--authorize-result" in argv:
            Path(argv[-1]).write_text(json.dumps({"bundle_identifier": APP_ID,
                "bundle_path": argv[argv.index("-a") + 1], "allowed": True, "authorization_status": 2, "error": ""}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @staticmethod
    def installed(app, identifier):
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "MacOS" / APP_EXECUTABLE).write_bytes(b"original notifier")
        data = plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleExecutable": APP_EXECUTABLE,
                              "CFBundleIconFile": "icon.icns"})
        (contents / "Info.plist").write_bytes(data)
        return data

    def test_migration_does_not_replace_legacy_sender_when_authorization_is_denied(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            original = self.installed(app, LEGACY_APP_ID)
            def denied(argv, **kwargs):
                if "--authorize-result" in argv:
                    self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                    Path(argv[-1]).write_text(json.dumps({"bundle_identifier": APP_ID, "allowed": False, "authorization_status": 1, "error": ""}))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return self.native(argv, **kwargs)
            with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=denied):
                with self.assertRaises(NotificationAuthorizationError):
                    install_notification_app(root, app)
            self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
            self.assertFalse(legacy_notifier_app(app).exists())
            self.assertEqual(bundle_identifier(pending_notifier_app(app)), APP_ID)

    def test_inconsistent_or_invalid_authorization_preserves_the_original(self):
        root = Path(__file__).resolve().parents[1]
        cases = [{"bundle_identifier": "other.sender"}, {"bundle_path": "/other.app"},
                 {"bundle_path": None}, {"authorization_status": 1},
                 {"authorization_status": None}, {"error": "native authorization error"},
                 [], "invalid JSON"]
        for invalid in cases:
            with self.subTest(result=invalid), tempfile.TemporaryDirectory() as temporary:
                app = Path(temporary) / "Hermes 学习.app"
                original = self.installed(app, LEGACY_APP_ID)

                def native(argv, **kwargs):
                    result = self.native(argv, **kwargs)
                    if "--authorize-result" in argv:
                        path = Path(argv[-1])
                        if isinstance(invalid, dict):
                            value = {**json.loads(path.read_text()), **invalid}
                            path.write_text(json.dumps(value))
                        else:
                            path.write_text(json.dumps(invalid) if isinstance(invalid, list) else invalid)
                    return result

                with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=native):
                    with self.assertRaises(NotificationAuthorizationError):
                        install_notification_app(root, app)
                self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                self.assertFalse(legacy_notifier_app(app).exists())

    def test_missing_authorization_result_does_not_replace_the_original(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            original = self.installed(app, LEGACY_APP_ID)

            def native(argv, **kwargs):
                if "--authorize-result" in argv:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return self.native(argv, **kwargs)

            with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=native), \
                    patch("hermes_codex_learning.macos_bundle.authorize_notification_app",
                          side_effect=lambda target, result: authorize_notification_app(target, result, timeout=0)):
                with self.assertRaises(NotificationAuthorizationError):
                    install_notification_app(root, app)
            self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
            self.assertFalse(legacy_notifier_app(app).exists())

    def test_retry_installs_the_fresh_build_instead_of_the_previous_candidate(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            original = self.installed(app, LEGACY_APP_ID)
            self.installed(pending_notifier_app(app), APP_ID)
            with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=self.native):
                install_notification_app(root, app)
            self.assertEqual((app / "Contents/MacOS" / APP_EXECUTABLE).read_bytes(), b"synthetic notifier")
            self.assertEqual((legacy_notifier_app(app) / "Contents/Info.plist").read_bytes(), original)

    def test_failed_registration_restores_the_original_and_retains_the_candidate(self):
        root = Path(__file__).resolve().parents[1]
        for identifier in (LEGACY_APP_ID, APP_ID):
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as temporary:
                app = Path(temporary) / "Hermes 学习.app"
                original = self.installed(app, identifier)
                failed = False

                def native(argv, **kwargs):
                    nonlocal failed
                    if Path(argv[0]).name == "lsregister" and argv[-1] == str(app) and not failed:
                        failed = True
                        raise subprocess.CalledProcessError(1, argv)
                    return self.native(argv, **kwargs)

                with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=native):
                    with self.assertRaises(subprocess.CalledProcessError):
                        install_notification_app(root, app)
                self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                self.assertEqual((app / "Contents/MacOS" / APP_EXECUTABLE).read_bytes(), b"original notifier")
                self.assertEqual(bundle_identifier(pending_notifier_app(app)), APP_ID)
                self.assertFalse(legacy_notifier_app(app).exists())

    def test_replacement_refuses_to_move_the_running_app(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            original = self.installed(app, APP_ID)
            def running(argv, **kwargs):
                if argv[1:2] == ["--prepare-upgrade"]:
                    self.assertEqual(argv[2], str(app))
                    self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                    raise subprocess.CalledProcessError(1, argv)
                return self.native(argv, **kwargs)
            with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=running):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_notification_app(root, app)
            self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)

    def test_successful_migration_preserves_original_bundle_and_registers_both_identities(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            original = self.installed(app, LEGACY_APP_ID)
            with patch("hermes_codex_learning.macos_bundle.subprocess.run", side_effect=self.native) as calls:
                result = install_notification_app(root, app)
            legacy = legacy_notifier_app(app)
            self.assertTrue(result["migrated"])
            self.assertEqual(bundle_identifier(app), APP_ID)
            self.assertEqual((legacy / "Contents/Info.plist").read_bytes(), original)
            self.assertEqual((legacy / "Contents/MacOS" / APP_EXECUTABLE).read_bytes(), b"original notifier")
            registrations = [call.args[0] for call in calls.call_args_list if Path(call.args[0][0]).name == "lsregister"]
            self.assertEqual(registrations[0][-1], str(pending_notifier_app(app)))
            self.assertEqual(registrations[-1][-2:], [str(legacy), str(app)])

    def test_ssh_installer_exports_readable_app_and_private_receiver_state(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)

            def native(argv, **kwargs):
                return self.native(argv, **kwargs)

            old_mask = os.umask(0o077)
            try:
                with patch.object(Path, "home", return_value=home), patch.object(sys, "platform", "darwin"), \
                     patch.object(sys, "argv", ["install_macos.py", "--host", "devbox", "--bridge", "/fixture/bridge", "--settings", "/fixture/settings"]), \
                     patch("subprocess.run", side_effect=native):
                    runpy.run_path(str(root / "desktop/install_macos.py"), run_name="__main__")
            finally:
                os.umask(old_mask)

            app = home / "Applications/Hermes 学习.app"
            self.assertEqual(app.parent.stat().st_mode & 0o777, 0o755)
            for relative in (".", "Contents", "Contents/MacOS", "Contents/Resources", "Contents/_CodeSignature"):
                self.assertEqual((app / relative).stat().st_mode & 0o777, 0o755, relative)
            self.assertEqual((app / "Contents/Info.plist").stat().st_mode & 0o777, 0o644)
            self.assertEqual((app / "Contents/MacOS/HermesLearningNotifier").stat().st_mode & 0o777, 0o755)
            self.assertEqual((app / "Contents/_CodeSignature/CodeResources").stat().st_mode & 0o777, 0o644)
            state = home / "Library/Application Support/HermesLearningSSH"
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((state / "config.json").read_text())["host"], "devbox")

    def test_normalization_leaves_unrelated_private_files_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Hermes 学习.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Resources").mkdir()
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.cy.hermes-learning-notifier",
                "CFBundleExecutable": "HermesLearningNotifier", "CFBundleIconFile": "icon.icns"}))
            (app / "Contents/MacOS/HermesLearningNotifier").write_bytes(b"fixture executable")
            (app / "Contents/Resources/icon.icns").write_bytes(b"fixture icon")
            private = app / "private-fixture.json"
            private.write_text("synthetic private fixture")
            private.chmod(0o600)
            normalize_bundle_permissions(app)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            self.assertEqual(private.read_text(), "synthetic private fixture")
            self.assertEqual((app / "Contents/Info.plist").stat().st_mode & 0o777, 0o644)
            self.assertEqual(normalize_bundle_permissions(app), {})


class MacOSNotificationRefreshTests(unittest.TestCase):
    def run_refresh(self, *, recover, enable):
        refresh = desktop_module("refresh_macos_notification")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            app = directory / "Hermes 学习.app"
            original = MacOSBundleTests.installed(app, LEGACY_APP_ID)
            refresh.__file__ = str(directory / "package/desktop/refresh_macos_notification.py")
            allowed = False
            decisions, settings, posts = [], [], []

            def native(argv, **kwargs):
                if "--authorize-result" in argv:
                    decisions.append(allowed)
                    Path(argv[-1]).write_text(json.dumps({
                        "bundle_identifier": APP_ID, "bundle_path": argv[argv.index("-a") + 1],
                        "allowed": allowed, "authorization_status": 2 if allowed else 1,
                        "error": "" if allowed else "Notifications are not allowed for this application",
                    }))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if Path(argv[0]).name == "open" and len(argv) == 2:
                    self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                    settings.append(argv[1])
                elif argv[1:2] == ["--post"]:
                    self.assertEqual(bundle_identifier(app), APP_ID)
                    posts.append(argv)
                    Path(argv[-1] + ".sent").write_text("accepted")
                return MacOSBundleTests.native(argv, **kwargs)

            def confirm(_prompt):
                nonlocal allowed
                self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)
                self.assertEqual(bundle_identifier(pending_notifier_app(app)), APP_ID)
                allowed = enable
                return ""

            arguments = [refresh.__file__] + (["--recover-authorization"] if recover else [])
            with patch.object(sys, "platform", "darwin"), patch.object(sys, "argv", arguments), \
                    patch.object(refresh, "sender_app", return_value=(app, "fixture sender")), \
                    patch.object(refresh, "diagnose", side_effect=lambda path, output: {
                        "bundle_identifier": bundle_identifier(path), "signature_verified": True}), \
                    patch("subprocess.run", side_effect=native), patch("builtins.input", side_effect=confirm), \
                    patch("builtins.print"):
                status = refresh.main()

            report = json.loads((directory / "package/report/notification-refresh-report.json").read_text())
            return {"status": status, "report": report, "decisions": decisions, "settings": settings, "posts": posts,
                    "identifier": bundle_identifier(app), "legacy": legacy_notifier_app(app).exists(),
                    "original_unchanged": (app / "Contents/Info.plist").read_bytes() == original}

    def test_settings_recovery_checks_fresh_authorization_before_migration_and_one_notice(self):
        result = self.run_refresh(recover=True, enable=True)
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["decisions"], [False, True])
        self.assertEqual(result["settings"], [
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=" + APP_ID])
        self.assertEqual(result["identifier"], APP_ID)
        self.assertTrue(result["legacy"])
        self.assertEqual(len(result["posts"]), 1)
        self.assertIs(result["report"]["authorization"]["allowed"], True)
        self.assertEqual(result["report"]["test"]["transport"], APP_ID)
        self.assertEqual(result["report"]["test"]["state"], "sent")
        self.assertIn("finished_at", result["report"])

    def test_still_denied_after_settings_keeps_the_original_and_posts_nothing(self):
        result = self.run_refresh(recover=True, enable=False)
        self.assertEqual(result["status"], 1)
        self.assertEqual(result["decisions"], [False, False])
        self.assertEqual(len(result["settings"]), 1)
        self.assertTrue(result["original_unchanged"])
        self.assertFalse(result["legacy"])
        self.assertEqual(result["posts"], [])
        self.assertIs(result["report"]["authorization"]["allowed"], False)
        self.assertEqual(result["report"]["state"], "failed")

    def test_denied_noninteractive_install_does_not_open_settings_or_send(self):
        result = self.run_refresh(recover=False, enable=False)
        self.assertEqual(result["status"], 1)
        self.assertEqual(result["decisions"], [False])
        self.assertEqual(result["settings"], [])
        self.assertTrue(result["original_unchanged"])
        self.assertEqual(result["posts"], [])


class MacOSIconRepairTests(unittest.TestCase):
    def test_denied_authorization_saves_a_report_without_posting(self):
        repair = desktop_module("repair_and_test_macos_icon")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            app = directory / "Hermes 学习.app"
            original = MacOSBundleTests.installed(app, LEGACY_APP_ID)
            repair.__file__ = str(directory / "package/desktop/repair_and_test_macos_icon.py")

            def native(argv, **kwargs):
                result = MacOSBundleTests.native(argv, **kwargs)
                if "--authorize-result" in argv:
                    path = Path(argv[-1])
                    path.write_text(json.dumps({**json.loads(path.read_text()),
                        "allowed": False, "authorization_status": 1}))
                self.assertNotIn("--post", argv)
                return result

            with patch.object(sys, "platform", "darwin"), patch.object(sys, "argv", [repair.__file__]), \
                    patch.object(repair, "sender_app", return_value=(app, "fixture sender")), \
                    patch("subprocess.run", side_effect=native), patch("builtins.print"):
                status = repair.main()
            report = json.loads((directory / "package/report/notification-icon-repair-report.json").read_text())
            self.assertEqual(status, 1)
            self.assertIs(report["authorization"]["allowed"], False)
            self.assertNotIn("notification_tests", report)
            self.assertEqual((app / "Contents/Info.plist").read_bytes(), original)

    def test_test_plans_from_another_or_unknown_sender_are_preserved_separately(self):
        repair = desktop_module("repair_and_test_macos_icon")
        for transport in (LEGACY_APP_ID, None):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                app = directory / "Hermes 学习.app"
                MacOSBundleTests.installed(app, APP_ID)
                old_receipt = directory / "old-receipt.json"
                old_receipt.write_text("historical receipt")
                plan_path = directory / "notification-icon-test-plan.json"
                old_plan = json.dumps({"marker": "OLD", "transport": transport, "icon_sha256": "same-icon",
                    "tests": [{"receipt": str(old_receipt), "state": "unknown"}]})
                plan_path.write_text(old_plan)

                def post(argv, **kwargs):
                    self.assertNotEqual(argv[-1], str(old_receipt))
                    Path(argv[-1] + ".sent").write_text("accepted")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with patch("subprocess.run", side_effect=post) as posts:
                    result = repair.post_tests(app, directory, "same-icon")
                self.assertFalse(result["reused"])
                self.assertEqual(result["transport"], APP_ID)
                self.assertEqual(posts.call_count, 2)
                archives = list(directory.glob("previous-icon-test-plan-*.json"))
                self.assertEqual([path.read_text() for path in archives], [old_plan])
                self.assertEqual(old_receipt.read_text(), "historical receipt")

    def test_current_sender_unknown_dispatch_is_reused_without_posting_again(self):
        repair = desktop_module("repair_and_test_macos_icon")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            app = directory / "Hermes 学习.app"
            MacOSBundleTests.installed(app, APP_ID)
            plan = {"marker": "CURRENT", "transport": APP_ID, "icon_sha256": "same-icon",
                    "tests": [{"receipt": str(directory / "receipt.json"), "state": "unknown"}]}
            (directory / "notification-icon-test-plan.json").write_text(json.dumps(plan))
            with patch("subprocess.run") as posts:
                result = repair.post_tests(app, directory, "same-icon")
            posts.assert_not_called()
            self.assertTrue(result["reused"])
            self.assertEqual(result["tests"][0]["state"], "unknown")
