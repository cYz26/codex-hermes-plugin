#!/usr/bin/env python3
"""Read official Codex hook trust without changing configuration or running hooks."""
from __future__ import annotations

import argparse
import json
import selectors
import shutil
import subprocess
import time
from pathlib import Path


PLUGIN = "hermes-learning@hermes-learning-local"
ROOT = Path(__file__).resolve().parents[1]


def inspect_hooks(executable: str, project: Path) -> dict:
    # An independent stdio process; never attach to Desktop's internal App Server.
    process = subprocess.Popen([executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()

    def send(value: dict) -> None:
        process.stdin.write((json.dumps(value) + "\n").encode())
        process.stdin.flush()

    def request(identifier: int, method: str, params: dict) -> dict:
        send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                message = json.loads(line)
                if message.get("id") == identifier:
                    if "error" in message:
                        raise RuntimeError(f"{method}: {message['error']}")
                    return message["result"]
            if not selector.select(min(0.5, max(0, deadline - time.monotonic()))):
                continue
            chunk = process.stdout.read(65536)
            if not chunk:
                raise RuntimeError("Codex diagnostic process closed before replying")
            pending.extend(chunk)
        raise TimeoutError(f"Codex did not reply to {method}")

    try:
        request(1, "initialize", {"clientInfo": {"name": "hermes_hook_diagnostic", "version": "1"},
                                  "capabilities": {"experimentalApi": True}})
        send({"method": "initialized"})
        result = request(2, "hooks/list", {"cwds": [str(project)]})
        entries = []
        for entry in result["data"]:
            hooks = [hook for hook in entry["hooks"] if hook.get("pluginId") == PLUGIN]
            entries.append({"cwd": entry["cwd"], "hooks": hooks,
                            "errors": entry["errors"], "warnings": entry["warnings"]})
        active = [hook for entry in entries for hook in entry["hooks"]]
        ready = (len(active) == 7 and not any(entry["errors"] for entry in entries)
                 and all(hook["enabled"] and hook["trustStatus"] in {"trusted", "managed"} for hook in active))
        return {"schema_version": 1, "plugin": PLUGIN, "project": str(project),
                "ready": ready, "scope": "fresh CLI configuration; actual Desktop execution must be checked separately",
                "trust_changed": False, "hooks_executed": False, "entries": entries}
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.codex:
        parser.error("Codex CLI was not found; specify --codex with the installed executable")
    result = inspect_hooks(args.codex, args.project.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
