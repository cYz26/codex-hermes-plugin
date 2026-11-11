"""CLI adapter for the same durable learning request service used by MCP."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Settings
from .ipc import request
from .learning_requests import MAX_CHUNK_BYTES


def add_parser(commands):
    learning = commands.add_parser("learning", help="Explicit session refinement and source learning")
    actions = learning.add_subparsers(dest="learning_action", required=True)
    for action in ("request", "material", "start", "status", "cancel"):
        parser = actions.add_parser(action)
        parser.add_argument("--settings")
        parser.add_argument("--cwd", required=True)
        parser.add_argument("--request-id", required=True)
        if action == "request":
            parser.add_argument("--chat", default=os.environ.get("CODEX_THREAD_ID"), help="Host chat ID; defaults to CODEX_THREAD_ID")
            parser.add_argument("--mode", choices=("refine", "learn"), default="refine")
            parser.add_argument("--focus", default="")
            parser.add_argument("--scope", choices=("both", "memory", "skills"), default="both")
            parser.add_argument("--memory-writes", choices=("add", "revise"), default="add")
            parser.add_argument("--through-turn")
            parser.add_argument("--skill-name")
        elif action == "material":
            parser.add_argument("--chunk-id", required=True)
            parser.add_argument("--source", required=True)
            parser.add_argument("--title")
            parser.add_argument("--file", type=Path, required=True, help="UTF-8 source chunk already selected by the user")


def execute(args):
    params = {"cwd":args.cwd, "request_id":args.request_id}
    action = args.learning_action
    if action == "request":
        if not args.chat:
            raise ValueError("Supply --chat when CODEX_THREAD_ID is unavailable; never select a chat by recency")
        params.update(chat_id=args.chat, mode=args.mode, focus=args.focus, scope=args.scope, memory_writes=args.memory_writes)
        for field in ("through_turn", "skill_name"):
            if getattr(args, field) is not None:
                params[field] = getattr(args, field)
    elif action == "material":
        with args.file.open("rb") as source:
            raw = source.read(MAX_CHUNK_BYTES + 1)
        if len(raw) > MAX_CHUNK_BYTES:
            raise ValueError("Source chunk exceeds 24 KiB; split it by topic first")
        params.update(chunk_id=args.chunk_id, source=args.source, content=raw.decode("utf-8"))
        if args.title:
            params["title"] = args.title
    method = {"request":"request_learning", "material":"add_learning_material", "start":"start_learning",
              "status":"learning_request_status", "cancel":"cancel_learning"}[action]
    result = request(Settings.load(args.settings), method, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
