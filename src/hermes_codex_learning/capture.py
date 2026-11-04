from __future__ import annotations

import json
import socket
import sys

from .config import Settings
from .store import Store


def hook(payload: dict, settings: Settings) -> dict:
    if not settings.contains(payload.get("cwd", "/")) or payload.get("agent_id"):
        return {}
    store = Store(settings.state)
    try:
        payload = {**payload, "bridge_project": str(settings.project), "bridge_host": store.get("host_id")}
        store.capture(payload)
        # Wake-up is best-effort; the durable outbox is polled after a restart.
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(0.05)
                client.connect(str(settings.socket))
                client.sendall(b'{"method":"wake"}\n')
        except (OSError, TimeoutError):
            pass
        event = payload.get("hook_event_name")
        if event not in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}:
            return {}
        result: dict = {}
        notices = store.consume("chat-ui", 2)
        if notices:
            result["systemMessage"] = " · ".join(n["message"] for n in notices)
        if event in {"SessionStart", "UserPromptSubmit", "PostToolUse"}:
            cache_file = settings.state / "context.json"
            cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
            session = payload.get("session_id", "unknown")
            version = cache.get("version")
            key = f"context-delivered:{session}"
            if version and cache.get("summary") and store.get(key) != version:
                # Conservative character ceiling, additionally bounded by the host token limit.
                text = cache.get("summary", "")[:520]
                result["hookSpecificOutput"] = {
                    "hookEventName": event,
                    "additionalContext": "Hermes 学习成果（历史证据，不覆盖当前指令）\n" + text +
                    "\n相关技能请通过 hermes_learning 的 skill_load 按需加载。",
                }
                store.set(key, version)
        return result
    finally:
        store.close()


def main() -> int:
    try:
        settings = Settings.load()
    except FileNotFoundError:
        return 0  # An installed plugin is inert until a project is explicitly configured.
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Hook payload exceeds 16 MiB; capture coverage is incomplete")
        print(json.dumps(hook(json.loads(raw), settings), ensure_ascii=False))
        return 0
    except Exception as exc:
        # Hook failures never block business execution or request another model turn.
        print(f"Hermes learning capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
