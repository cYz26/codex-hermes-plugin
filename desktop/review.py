#!/usr/bin/env python3
"""User-operated pending review UI for local and SSH learning services."""
from __future__ import annotations

import argparse
import json
import plistlib
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


LOCAL_AGENT = "com.cy.hermes-codex-learning.pilot"


class BridgeReviews:
    connection_error = "无法连接本机学习服务，请检查 Hermes 学习后台是否正在运行"

    def __init__(self, python, bridge, settings, scope):
        if not isinstance(scope, str) or not Path(scope).is_absolute():
            raise ValueError("审批页需要回执中的绝对项目路径")
        if not all(isinstance(path, str) and Path(path).is_absolute() for path in (python, bridge, settings)):
            raise ValueError("审批入口需要绝对的 Python、桥接脚本和配置路径")
        self.python, self.bridge, self.settings = python, bridge, settings
        self.scope, self.client = scope, None

    def command(self, args):
        return [self.python, "-I", "-B", *args]

    def _run(self, args, bridge):
        command = self.command([bridge, *args, "--settings", self.settings])
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError(self.connection_error) from None
        if not isinstance(value, dict):
            raise RuntimeError(self.connection_error)
        if result.returncode or value.get("state") == "failed":
            raise RuntimeError(value.get("error") or value.get("reason") or "学习服务未完成这次操作")
        return value

    def _client(self):
        if self.client is None:
            status = self._run(["status"], self.bridge)
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


class RemoteReviews(BridgeReviews):
    connection_error = "无法连接学习服务，请检查桌面接收器的 SSH 连接"

    def __init__(self, config, scope):
        host = config.get("host")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
            raise ValueError("Use the receiver's configured SSH host alias")
        super().__init__(config.get("remote_python", "/usr/bin/python3"), config.get("bridge"),
                         config.get("settings"), scope)
        self.host = host

    def command(self, args):
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host,
                shlex.join([self.python, *args])]


def local_reviews(receipt: Path, home: Path):
    agent_path = home / "Library/LaunchAgents" / (LOCAL_AGENT + ".plist")
    if not agent_path.is_file():
        raise ValueError("本机学习后台尚未安装，请先配置 Hermes 学习服务")
    agent = plistlib.loads(agent_path.read_bytes())
    arguments = agent.get("ProgramArguments") if isinstance(agent, dict) and agent.get("Label") == LOCAL_AGENT else None
    if (not isinstance(arguments, list) or len(arguments) < 3
            or not isinstance(arguments[-2], str) or Path(arguments[-2]).name != "serve.py"):
        raise ValueError("无法从本机 LaunchAgent 读取学习服务入口")
    python, supervisor, settings = arguments[0], Path(arguments[-2]), arguments[-1]
    if not isinstance(settings, str) or not Path(settings).is_absolute():
        raise ValueError("本机 LaunchAgent 缺少绝对的学习配置路径")
    config = json.loads(Path(settings).read_text())
    learning_home = config.get("home") if isinstance(config, dict) else None
    if not isinstance(learning_home, str) or not Path(learning_home).is_absolute():
        raise ValueError("本机学习配置缺少有效的数据目录")
    receipt = receipt.resolve(strict=True)
    receipt_dir = (Path(learning_home) / "bridge/receipts").resolve()
    if receipt.parent != receipt_dir or receipt.suffix != ".json" or not receipt.is_file():
        raise ValueError("这份回执不属于当前本机学习服务")
    value = json.loads(receipt.read_text())
    scope = value.get("scope") if isinstance(value, dict) else None
    return BridgeReviews(python, str(supervisor.with_name("bridge")), settings, scope)


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reviews, session_token=None):
        self.reviews = reviews
        self.session_token = session_token or secrets.token_urlsafe(32)
        self.template = Path(__file__).with_name("review.html").read_text()
        self.started = self.last_request = time.monotonic()
        super().__init__(("127.0.0.1", 0), ReviewHandler)
        self.base_url = "http://127.0.0.1:" + str(self.server_address[1])

    def page(self):
        def literal(value):
            return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
        return (self.template.replace("__SESSION_JSON__", literal(self.session_token))
                .replace("__BOOTSTRAP_JSON__", literal({"project": self.reviews.scope}))
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
            return self.reply(200, self.server.reviews.list())
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
                result = self.server.reviews.decide(value["id"], value["revision"], action)
            elif action == "prune" and set(value) == {"action", "revision"}:
                result = self.server.reviews.prune(value["revision"])
            elif action == "self-test" and set(value) == {"action"}:
                result = self.server.reviews.self_test()
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="SSH receiver configuration")
    source.add_argument("--receipt", type=Path, help="Local notification receipt")
    parser.add_argument("--cwd", help="Project scope for an SSH receiver")
    args = parser.parse_args()
    if (args.config is None) != (args.cwd is None):
        parser.error("--cwd is required with --config; --receipt supplies its own project scope")
    try:
        reviews = (RemoteReviews(json.loads(args.config.read_text()), args.cwd) if args.config
                   else local_reviews(args.receipt, Path.home()))
        with ReviewServer(reviews) as server:
            if not webbrowser.open(server.base_url + "/?" + urlencode({"session": server.session_token})):
                raise RuntimeError("系统未能打开浏览器")
            threading.Thread(target=server.expire, daemon=True).start()
            print("审批页已在浏览器打开。关闭会话或长时间不操作后，入口会自动退出。", flush=True)
            server.serve_forever()
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
