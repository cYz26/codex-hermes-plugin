#!/usr/bin/env python3
"""Read live App receipts for two fresh, tool-created project recall chats."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_codex_learning import __version__
from hermes_codex_learning.capture import ADDITIONAL_CONTEXT_LIMIT
from hermes_codex_learning.config import Settings
from hermes_codex_learning.delegation import _result_object
from hermes_codex_learning.store import atomic_json

CANARY = re.compile(r"\bSCOPE-[AB]-[0-9A-F]{8}\b")
HOOK_PREFIX = "Hermes 全局自动学习已启用；当前项目："
CREATE_TOOL = "mcp__codex_app__create_thread"
CONTEXT_TOOL = "mcp__hermes_learning__context"
SKILL_TOOL = "mcp__hermes_learning__skill_load"


class Pending(Exception):
    """A real App or worker event is still outstanding."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def label_digests(text):
    return {sha256(label) for label in CANARY.findall(text)}


def readonly(path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def creation_receipt(db, chat, marker):
    matches = []
    for identity, raw, delivered in db.execute(
        "SELECT id,payload,delivered FROM events WHERE json_extract(payload,'$.hook_event_name')='PostToolUse' "
        "AND json_extract(payload,'$.tool_name')=? AND instr(payload,?)>0", (CREATE_TOOL, chat)
    ):
        payload = json.loads(raw)
        result = _result_object(payload.get("tool_response"))
        if result and result.get("threadId") == chat:
            matches.append((identity, payload, delivered, result.get("hostId")))
    if not matches:
        raise Pending("App creation receipt is still missing")
    require(len(matches) == 1, "Ambiguous App creation receipts for the destination chat")
    identity, payload, delivered, host_id = matches[0]
    args = payload["tool_input"]
    target = args["target"]
    require(host_id == "local" and target.get("type") == "project"
            and target.get("environment", {}).get("type") == "local", "Expected a local App project chat")
    require(args["prompt"].splitlines()[0] == marker, "Creation receipt does not match the recall run marker")
    require(not label_digests(args["prompt"]), "Creation prompt supplied a test label")
    return identity, payload, delivered


def injected_context(start, chat, project, foreign):
    path = Path(start["transcript_path"]).resolve()
    codex_home = Path.home().joinpath(".codex").resolve()
    require(chat in path.name and path.is_relative_to(codex_home), "Unexpected App transcript path")
    context = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # The App can still be appending the final line.
            require(foreign not in label_digests(line), "Foreign test label appeared in the recall transcript")
            payload = item.get("payload", {})
            if item.get("type") == "response_item" and payload.get("role") == "developer":
                context.extend(part["text"] for part in payload.get("content", [])
                               if isinstance(part, dict) and part.get("text", "").startswith(HOOK_PREFIX))
    require(context and all(text.splitlines()[0] == HOOK_PREFIX + project for text in context),
            "The App did not deliver the expected project context")
    require(all(len(text.encode("utf-8")) <= ADDITIONAL_CONTEXT_LIMIT for text in context),
            "An App hook context exceeded the byte-level token upper bound")
    return context


def tool_receipts(events, project, own, foreign, settings):
    guide = (Path.home() / ".codex/plugins/cache/hermes-learning-local/hermes-learning" / __version__
             / "skills/hermes-learned-experience/SKILL.md")
    reads = []
    for identity, event, _ in events:
        if event.get("hook_event_name") != "PostToolUse":
            continue
        name = event.get("tool_name")
        args = event.get("tool_input", {})
        response = event.get("tool_response")
        returned = label_digests(json.dumps(response, ensure_ascii=False))
        require(foreign not in returned, "A tool returned the other project's test label")
        receipt = {"event": identity, "tool": name, "call_id": event.get("tool_use_id")}
        require(receipt["call_id"], "Missing App tool-call identity")
        if name in {CONTEXT_TOOL, SKILL_TOOL}:
            value = _result_object(response)
            require(value and settings.project_for(args.get("cwd")) == project,
                    "Recall called Hermes without its own project scope")
            if name == CONTEXT_TOOL:
                require(value.get("project") == project, "Hermes returned another project scope")
                receipt["own_label_returned"] = own in label_digests(json.dumps(value.get("memories", {})))
            else:
                require(not returned, "Shared skill content included a test label")
        else:
            command = shlex.split(args.get("command", "")) if name == "Bash" else []
            require(command in [["pwd"], ["cat", str(guide)]] and not returned,
                    "Recall ran a tool outside the learning and workspace checks")
        reads.append(receipt)
    return reads


def verify_chat(db, journal, chat, marker, learned, foreign, settings):
    project, own = learned["project"], learned["expected_label_sha256"]
    rows = db.execute("SELECT id,payload,delivered FROM events WHERE json_extract(payload,'$.session_id')=? "
                      "ORDER BY created,id", (chat,))
    events = [(identity, json.loads(raw), bool(delivered)) for identity, raw, delivered in rows]
    starts = [event for _, event, _ in events if event["hook_event_name"] == "SessionStart"]
    stops = [(identity, event) for identity, event, _ in events if event["hook_event_name"] == "Stop"]
    if not stops:
        raise Pending("Recall chat is waiting for its App Stop hook")
    require(len(starts) == 1 and len(stops) == 1, "Expected a fresh chat with one terminal turn")
    stop_id, stop = stops[0]
    turn = stop["turn_id"]
    source_id, parent, parent_delivered = creation_receipt(db, chat, marker)
    require(all(event.get("bridge_project") == project and event.get("bridge_host") == parent["bridge_host"]
                and event.get("turn_id") in (None, turn) for _, event, _ in events), "App event scope mismatch")
    final = stop["last_assistant_message"].strip().strip("`").strip()
    require(sha256(final) == own, "The chat did not return its complete project label")
    context = injected_context(starts[0], chat, project, foreign)
    from_hook = own in label_digests("\n".join(context))
    reads = tool_receipts(events, project, own, foreign, settings)
    require(from_hook or any(read.get("own_label_returned") for read in reads), "No scoped source for the answer")
    projection = journal.execute("SELECT projection,body FROM observations WHERE id=? AND turn=?",
                                 (stop_id, turn)).fetchone()
    if not parent_delivered or not all(delivered for _, _, delivered in events) or not projection:
        raise Pending("Recall succeeded in the App; the worker has not projected its turn yet")
    state, raw = projection
    observation = json.loads(raw)
    require(state == "projected" and observation["source"]["project"] == project
            and observation["source"]["chat"] == chat, "Hermes projection did not preserve the App identity")
    require(f"delegated_task_input:{source_id}" in observation["coverage"], "The projection lost the creation receipt")
    return {
        "chat": chat, "turn": turn, "project": project, "label_sha256": own,
        "training_chat": learned["chat"], "source_event": source_id, "parent_chat": parent["session_id"],
        "parent_turn": parent["turn_id"], "parent_tool_call": parent["tool_use_id"],
        "stop_event": stop_id, "projection": state, "recall_path": "hook" if from_hook else "scoped_mcp",
        "hook_context_sha256": [sha256(text) for text in context], "tools": reads,
        "hook_context_bytes": [len(text.encode("utf-8")) for text in context],
        "foreign_label_in_transcript": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-a", required=True)
    parser.add_argument("--chat-b", required=True)
    parser.add_argument("--marker", default="HER-021-RECALL-AUTO")
    parser.add_argument("--training", type=Path, help="Defaults to the training evidence in ACCEPTANCE.json")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.settings)
    acceptance = json.loads((ROOT / "ACCEPTANCE.json").read_text(encoding="utf-8"))
    path = args.training or ROOT / acceptance["cross_project_verification"]["training_evidence"]
    learned = json.loads(path.read_text(encoding="utf-8"))["projects"]
    chats = {"A": args.chat_a, "B": args.chat_b}
    require(len({*chats.values(), learned["A"]["chat"], learned["B"]["chat"]}) == 4,
            "Recall must use two chats distinct from the training chats")
    deadline = time.monotonic() + max(0, args.wait_seconds)
    while True:
        try:
            with readonly(settings.state / "bridge.sqlite") as db, readonly(settings.home / "learning/journal.sqlite") as journal:
                result = {lane: verify_chat(db, journal, chat, args.marker, learned[lane],
                                            learned["B" if lane == "A" else "A"]["expected_label_sha256"], settings)
                          for lane, chat in chats.items()}
            break
        except Pending as exc:
            if time.monotonic() >= deadline:
                print(json.dumps({"status": "pending", "reason": str(exc)}, ensure_ascii=False))
                return 2
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    evidence = {"status": "passed", "version": __version__, "marker": args.marker,
                "origin": "Codex_App_create_thread_with_live_hooks", "projects": result}
    output = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        atomic_json(args.out, evidence)
    print(output, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
