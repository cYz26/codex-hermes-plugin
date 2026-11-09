import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge


class NotificationTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "darwin", "Non-macOS service behavior")
    def test_non_macos_never_dispatches_osascript_even_with_legacy_notifications_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = Bridge(Settings(root, root, root, Path(sys.executable), notifications=True), object())
            try:
                bridge.store.notice({"id": "fixture", "kind": "review.finished", "run_status": "completed",
                                     "applied": [{"name": "fixture"}], "message": "Synthetic result"})
                with patch("hermes_codex_learning.daemon.macos_notification", return_value={"dispatched": False}) as notify:
                    bridge.notifications()
                notify.assert_not_called()
            finally:
                bridge.store.close()
