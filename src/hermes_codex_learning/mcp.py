"""Small stdio MCP client surface; no Agent, database or credential access."""
import json
import sys

from .config import Settings
from .ipc import request
from . import __version__

TOOLS = [
    {"name": "learning_status", "description": "Read Hermes learning progress, applied/staged changes and actual model usage. Does not trigger learning.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "context", "description": "Read the pilot project's learned memory and available skill versions. Listing does not count as skill use.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "skill_load", "description": "Load the complete, current Hermes learned skill and supporting resources when relevant to the task. Returns a version and delivery receipt.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}},
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
