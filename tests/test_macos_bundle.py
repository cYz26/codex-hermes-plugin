import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from hermes_codex_learning.macos_bundle import (APP_ID, LEGACY_APP_ID, APP_EXECUTABLE,
    NotificationAuthorizationError, authorize_notification_app, bundle_identifier, install_notification_app,
    legacy_notifier_app, normalize_bundle_permissions, pending_notifier_app)


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
