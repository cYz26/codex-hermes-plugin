#!/usr/bin/env python3
"""User-operated pending review UI over the receiver's existing SSH connection."""
from __future__ import annotations

import json
import argparse
import re
import secrets
import shlex
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


class RemoteReviews:
    def __init__(self, config, scope):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", config["host"]):
            raise ValueError("Use the receiver's configured SSH host alias")
        if not isinstance(scope, str) or not Path(scope).is_absolute():
            raise ValueError("An absolute project scope is required")
        for key in ("bridge", "settings", "remote_python"):
            if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
                raise ValueError("The receiver configuration needs absolute bridge, settings and Python paths")
        self.config, self.scope, self.client = config, scope, None

    def _run(self, args, bridge):
        command = shlex.join([self.config["remote_python"], bridge, *args, "--settings", self.config["settings"]])
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                 self.config["host"], command], capture_output=True, text=True, timeout=60)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("无法连接学习服务，请检查桌面接收器的 SSH 连接") from None
        if result.returncode or value.get("state") == "failed":
            raise RuntimeError(value.get("error") or value.get("reason") or "学习服务未完成这次操作")
        return value

    def _client(self):
        if self.client is None:
            status = self._run(["status"], self.config["bridge"])
            root = status.get("plugin_root")
            if (status.get("approval_capabilities", {}).get("revision_checked_decisions") is not True
                    or not isinstance(root, str) or not Path(root).is_absolute()):
                raise RuntimeError("当前学习服务没有启用审批入口，请先安装包含审批流程的版本")
            self.client = str(Path(root) / "scripts/bridge")
        return self.client

    def list(self):
        return self._run(["approvals", "list", "--cwd", self.scope], self._client())

    def decide(self, identifier, revision, decision):
        if (decision not in {"approve", "reject"} or not re.fullmatch(r"[a-f0-9]{8}", identifier)
                or not re.fullmatch(r"[a-f0-9]{64}", revision)):
            raise ValueError("请先查看这条提案的差异，再提交明确决定")
        return self._run(["approvals", decision, "--cwd", self.scope, "--id", identifier,
                          "--revision", revision], self._client())

    def prune(self, revision):
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("请刷新并查看清理预览")
        return self._run(["approvals", "prune", "--cwd", self.scope, "--revision", revision, "--apply"], self._client())

    def self_test(self):
        return self._run(["approvals", "self-test"], self._client())


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, remote, session_token=None):
        self.remote = remote
        self.session_token = session_token or secrets.token_urlsafe(32)
        self.template = Path(__file__).with_name("review.html").read_text()
        self.started = self.last_request = time.monotonic()
        super().__init__(("127.0.0.1", 0), ReviewHandler)
        self.base_url = "http://127.0.0.1:" + str(self.server_address[1])

    def page(self):
        def literal(value):
            return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
        return (self.template.replace("__SESSION_JSON__", literal(self.session_token))
                .replace("__BOOTSTRAP_JSON__", literal({"project": self.remote.scope}))
                .replace("__CSP_NONCE__", self.session_token)).encode()

    def expire(self):
        while time.monotonic() - self.started < 3600 and time.monotonic() - self.last_request < 1800:
            time.sleep(10)
        self.shutdown()


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Never log session URLs, proposal bodies or SSH arguments.
        return

    def host_allowed(self):
        return self.headers.get("Host") == urlsplit(self.server.base_url).netloc

    def api_allowed(self, writing=False):
        return (self.host_allowed() and secrets.compare_digest(
            self.headers.get("X-Hermes-Review-Token", ""), self.server.session_token)
            and (not writing or self.headers.get("Origin") == self.server.base_url))

    def reply(self, status, value, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; "
                         "script-src 'nonce-" + self.server.session_token + "'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path)
        if not self.host_allowed():
            return self.reply(403, {"error": "本地审批会话不匹配"})
        if path.path == "/":
            if parse_qs(path.query).get("session") != [self.server.session_token]:
                return self.reply(403, {"error": "请从通知窗口重新打开审批页"})
            self.server.last_request = time.monotonic()
            return self.reply(200, self.server.page(), "text/html; charset=utf-8")
        if path.path != "/api/proposals" or not self.api_allowed():
            return self.reply(403, {"error": "审批会话不匹配"})
        self.server.last_request = time.monotonic()
        try:
            return self.reply(200, self.server.remote.list())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            return self.reply(502, {"error": str(error)})

    def do_POST(self):
        if not self.api_allowed(writing=True):
            return self.reply(403, {"error": "请在当前审批页中提交决定"})
        self.server.last_request = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                return self.reply(413, {"error": "审批请求过大"})
            self.connection.settimeout(10)
            value = json.loads(self.rfile.read(length) or "{}")
            if urlsplit(self.path).path == "/api/close" and value == {}:
                self.reply(200, {"closed": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if urlsplit(self.path).path != "/api/decision" or not isinstance(value, dict):
                raise ValueError("审批请求格式不正确")
            action = value.get("action")
            if action in {"approve", "reject"} and set(value) == {"action", "id", "revision"}:
                result = self.server.remote.decide(value["id"], value["revision"], action)
            elif action == "prune" and set(value) == {"action", "revision"}:
                result = self.server.remote.prune(value["revision"])
            elif action == "self-test" and set(value) == {"action"}:
                result = self.server.remote.self_test()
            else:
                raise ValueError("审批请求只接受当前页显示的编号、版本和决定")
            self.reply(200, result)
        except (ValueError, TypeError) as error:
            self.reply(400, {"error": str(error)})
        except RuntimeError as error:
            self.reply(409, {"error": str(error)})
        except (OSError, subprocess.SubprocessError):
            self.reply(502, {"error": "操作结果尚未确认；请先刷新状态，再决定是否重试"})


def main():
    parser = argparse.ArgumentParser(description="Open the project-scoped Hermes approval page")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cwd", required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    config.setdefault("remote_python", "/usr/bin/python3")
    server = ReviewServer(RemoteReviews(config, args.cwd))
    threading.Thread(target=server.expire, daemon=True).start()
    webbrowser.open(server.base_url + "/?" + urlencode({"session": server.session_token}))
    print("审批页已在浏览器打开。关闭会话或长时间不操作后，入口会自动退出。", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
