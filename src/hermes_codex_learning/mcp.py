"""Small stdio MCP client surface; no Agent, database or credential access."""
import json
import sys

from .config import Settings
from .ipc import request
from . import __version__

TOOLS = [
    {"name": "pending_reviews", "description": "Inspect project-scoped pending memory proposals and their diffs. Does not approve, reject or run a review.",
     "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}, "id": {"type": "string"}},
                     "required": ["cwd"], "additionalProperties": False}},
    {"name":"record_application", "description":"Record a delivered skill's application claim and host test references. Does not run reviews; a passing test does not prove causation.",
     "inputSchema":{"type":"object","properties":{"delivery_token":{"type":"string"},"cwd":{"type":"string"},
        "stage":{"type":"string","enum":["applied","verified","failed"]},
        "source_events":{"type":"array","maxItems":20,"items":{"type":"object",
          "properties":{"chat":{"type":"string"},"turn":{"type":"string"},"tool_call":{"type":"string"}},
          "required":["chat","turn","tool_call"],"additionalProperties":False}}},
        "required":["delivery_token","cwd","stage"],"additionalProperties":False}},
    {"name": "feedback_status", "description": "Read the latest turn's skill versions and copyable test references, or read one application receipt. Pass the absolute cwd and the host chat ID.",
     "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}, "chat": {"type": "string"},
                     "turn": {"type": "string"}, "receipt_id": {"type": "string"}},
                     "required": ["cwd", "chat"], "additionalProperties": False}},
    {"name": "update_status", "description": "Read automatic Hermes update policy, latest check, candidate compatibility gates and activation/rollback outcome. Works when the learning worker is offline.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "update_check", "description": "Check the official Hermes stable release and exact plugin compatibility. Does not install or activate a release.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "learning_status", "description": "Read capture, review, publication, recall and feedback progress. With cwd and chat, execution.test_references lists host event references for record_application. Use turn or after_cursor to narrow results. Does not trigger learning.",
     "inputSchema": {"type": "object", "properties": {"cwd":{"type":"string"},"chat":{"type":"string"},"turn":{"type":"string"},
        "limit":{"type":"integer","minimum":1,"maximum":100},"after_cursor":{"type":"integer","minimum":0}}, "additionalProperties": False}},
    {"name": "context", "description": "Read Hermes personal knowledge: project-scoped memories and relevant shared skills. Pass the current task's absolute cwd and a short task query; omitted cwd returns no project memory. Listing is not skill use.",
     "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}, "query": {"type": "string"}}, "additionalProperties": False}},
    {"name": "skill_load", "description": "Load a current shared Hermes skill/resource. Pass cwd and the artifact_version from context to require an exact version. A delivery receipt does not prove adoption.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"},
                     "cwd": {"type": "string"}, "artifact_version": {"type": "string"}},
                     "required": ["name"], "additionalProperties": False}},
]

from .learning_tools import TOOLS as LEARNING_TOOLS
TOOLS.extend(LEARNING_TOOLS)

for tool in TOOLS:
    readonly=tool["name"] in {"context","skill_load","learning_status","feedback_status","update_status","learning_request_status","pending_reviews"}
    tool["annotations"]={"readOnlyHint":readonly,"destructiveHint":False,
                         "openWorldHint":tool["name"]=="update_check"}
    if tool["name"] in {"record_application", *(item["name"] for item in LEARNING_TOOLS)}:
        tool["annotations"]["idempotentHint"]=True


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
            updater = Updater(settings,create_directory=name!="update_status")
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
