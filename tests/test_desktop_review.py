import importlib.util
import http.client
import json
import plistlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class DesktopReviewTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[1] / "desktop/review.py"
        self.assertTrue(path.is_file(), "The desktop review entry point is missing")
        spec = importlib.util.spec_from_file_location("desktop_review", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_existing_receiver_configuration_resolves_the_active_approval_client(self):
        review = self.module()
        config = {"host": "devbox", "remote_python": "/usr/bin/python3", "bridge": "/old/bridge",
                  "settings": "/learning/bridge.json"}
        status = {"plugin_root": "/active/plugin", "approval_capabilities": {"revision_checked_decisions": True}}
        answers = [SimpleNamespace(returncode=0, stdout=json.dumps(status)),
                   SimpleNamespace(returncode=0, stdout=json.dumps({"proposals": []}))]
        with patch.object(review.subprocess, "run", side_effect=answers) as execute:
            client = review.RemoteReviews(config, "/project")
            self.assertEqual(client.list()["proposals"], [])
        first, second = [call.args[0] for call in execute.call_args_list]
        self.assertIn("/old/bridge status", first[-1])
        self.assertIn("/active/plugin/scripts/bridge approvals list", second[-1])
        self.assertIn("--cwd /project", second[-1])
        self.assertEqual(first[:2], ["ssh", "-T"])

    def test_local_receipt_binds_the_active_bridge_to_its_project_and_learning_home(self):
        review = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            learning_home = home / "Local learning data"
            receipt = learning_home / "bridge/receipts/history.json"
            receipt.parent.mkdir(parents=True)
            scope = str(home / "Workspace with spaces")
            receipt.write_text(json.dumps({"summary": "Existing receipt", "text": "Detail", "scope": scope}))
            settings = home / "active settings.json"
            settings.write_text(json.dumps({"home": str(learning_home)}))
            agent = home / "Library/LaunchAgents" / (review.LOCAL_AGENT + ".plist")
            agent.parent.mkdir(parents=True)
            python = "/stable/python3"
            agent.write_bytes(plistlib.dumps({"Label": review.LOCAL_AGENT, "ProgramArguments": [
                python, "-I", "-B", "/bootstrap/scripts/serve.py", str(settings)]}))

            client = review.local_reviews(receipt, home)
            status = {"plugin_root": "/active/plugin", "approval_capabilities": {"revision_checked_decisions": True}}
            answers = [SimpleNamespace(returncode=0, stdout=json.dumps(value)) for value in (
                status, {"proposals": []}, {"state": "rejected"})]
            with patch.object(review.subprocess, "run", side_effect=answers) as execute:
                self.assertEqual(client.list()["proposals"], [])
                self.assertEqual(client.decide("11111111", "a" * 64, "reject")["state"], "rejected")
            common = [python, "-I", "-B"]
            active = [*common, "/active/plugin/scripts/bridge", "approvals"]
            config = ["--settings", str(settings)]
            self.assertEqual([call.args[0] for call in execute.call_args_list], [
                [*common, "/bootstrap/scripts/bridge", "status", *config],
                [*active, "list", "--cwd", scope, *config],
                [*active, "reject", "--cwd", scope, "--id", "11111111", "--revision", "a" * 64, *config],
            ])

            outside = home / "unbound-receipt.json"
            outside.write_bytes(receipt.read_bytes())
            with self.assertRaisesRegex(ValueError, "不属于当前本机学习服务"):
                review.local_reviews(outside, home)

    def server(self):
        review = self.module()
        self.assertTrue(hasattr(review, "ReviewServer"), "The user-operated review server is missing")
        remote = SimpleNamespace(scope="/project", list=Mock(return_value={"project": "/project", "proposals": []}),
                                 decide=Mock(return_value={"state": "approved", "applied": True}),
                                 prune=Mock(), self_test=Mock())
        server = review.ReviewServer(remote, session_token="fixture-session")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return server, remote

    def request(self, server, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        body = json.dumps(value) if value is not None else None
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_preview_is_readonly_and_actions_need_the_displayed_session_and_origin(self):
        server, remote = self.server()
        headers = {"X-Hermes-Review-Token": "fixture-session"}
        status, body, _ = self.request(server, "GET", "/api/proposals", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["proposals"], [])
        remote.decide.assert_not_called()
        value = {"action": "approve", "id": "11111111", "revision": "a" * 64}
        status, _, _ = self.request(server, "POST", "/api/decision", value, headers)
        self.assertEqual(status, 403)
        remote.decide.assert_not_called()
        headers["Origin"] = server.base_url
        status, body, _ = self.request(server, "POST", "/api/decision", value, headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["applied"])
        remote.decide.assert_called_once_with("11111111", "a" * 64, "approve")

    def test_another_project_cannot_be_injected_into_a_decision(self):
        server, remote = self.server()
        headers = {"X-Hermes-Review-Token": "fixture-session", "Origin": server.base_url}
        value = {"action": "approve", "id": "11111111", "revision": "a" * 64, "cwd": "/foreign"}
        status, _, _ = self.request(server, "POST", "/api/decision", value, headers)
        self.assertEqual(status, 400)
        remote.decide.assert_not_called()

    def test_page_requires_the_session_and_safely_embeds_project_names(self):
        server, remote = self.server()
        remote.scope = "/project</script><script>alert(1)</script>"
        status, _, _ = self.request(server, "GET", "/")
        self.assertEqual(status, 403)
        status, body, headers = self.request(server, "GET", "/?session=fixture-session")
        self.assertEqual(status, 200)
        self.assertNotIn(b"</script><script>alert(1)", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
