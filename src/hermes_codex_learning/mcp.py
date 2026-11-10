"""Small stdio MCP client surface; no Agent, database or credential access."""
import json
import sys

from .config import Settings
from .ipc import request
from . import __version__

TOOLS = [
    {"name":"record_application", "description":"Record a delivered skill's application claim and host test references. Does not run reviews; a passing test does not prove causation.",
     "inputSchema":{"type":"object","properties":{"delivery_token":{"type":"string"},"cwd":{"type":"string"},
        "stage":{"type":"string","enum":["applied","verified","failed"]},
        "source_events":{"type":"array","maxItems":20,"items":{"type":"object",
          "properties":{"chat":{"type":"string"},"turn":{"type":"string"},"tool_call":{"type":"string"}},
          "required":["chat","turn","tool_call"],"additionalProperties":False}}},
        "required":["delivery_token","cwd","stage"],"additionalProperties":False}},
    {"name": "update_status", "description": "Read automatic Hermes update policy, latest check, candidate compatibility gates and activation/rollback outcome. Works when the learning worker is offline.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "update_check", "description": "Check the official Hermes stable release and exact plugin compatibility. Does not install or activate a release.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "learning_status", "description": "Read Hermes learning progress, applied/staged changes and actual model usage. Does not trigger learning.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "context", "description": "Read Hermes personal knowledge: project-scoped memories and relevant shared skills. Pass the current task's absolute cwd and a short task query; omitted cwd returns no project memory. Listing is not skill use.",
     "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}, "query": {"type": "string"}}, "additionalProperties": False}},
    {"name": "skill_load", "description": "Load a current shared Hermes skill/resource with version and source provenance. Pass the task's absolute cwd; check applicability of project-specific examples. A delivery receipt does not prove adoption.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}, "cwd": {"type": "string"}},
                     "required": ["name"], "additionalProperties": False}},
]


def handle(message: dict, settings: Settings) -> dict | None:
    if "id" not in message:
        return None
    method = message["method"]
    if method == "initialize":
        return {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "hermes-learning", "version": __version__}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params", {})
        name = params["name"]
        if name not in {t["name"] for t in TOOLS}:
            raise ValueError("Unknown tool")
        if name in {"update_status", "update_check"}:
            from .updates import Updater
            updater = Updater(settings)
            result = updater.status() if name == "update_status" else updater.check()
        else:
            result = request(settings, name, params.get("arguments", {}))
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}
    raise ValueError("Unsupported MCP method")


def main() -> int:
    settings = Settings.load()
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            result = handle(message, settings)
            if result is None:
                continue
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0
