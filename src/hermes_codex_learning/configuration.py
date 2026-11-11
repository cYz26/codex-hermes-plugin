"""Small native configuration builder; no runtime or credential imports."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def build_native_config(*, version, provider: str = "openai-codex", model: str = "gpt-6.1-sol",
                        api_mode: str = "codex_responses", base_url: str | None = None,
                        reasoning_effort: str = "max", session_affinity_header: str | None = None) -> dict:
    if not all(isinstance(value, str) and value.strip() for value in
               (provider, model, api_mode, reasoning_effort)):
        raise ValueError("Provider, model, API mode and reasoning effort are required")
    if api_mode not in {"codex_responses", "chat_completions", "anthropic_messages"}:
        raise ValueError("Unsupported native API mode")
    if base_url:
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(char.isspace() for char in base_url)):
            raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query or fragment")
    custom = provider.startswith("custom:")
    if custom and (not re.fullmatch(r"custom:[A-Za-z0-9_.-]+", provider) or not base_url):
        raise ValueError("A named custom provider requires an explicit endpoint")
    if session_affinity_header and (not custom or not re.fullmatch(r"[A-Za-z0-9-]+", session_affinity_header)):
        raise ValueError("Session affinity requires a named custom provider and a valid header name")
    config = {
        "_config_version": version,
        "model": {"provider": provider, "default": model, "api_mode": api_mode},
        "agent": {"reasoning_effort": reasoning_effort},
        "auth": {"adopt_external_logins": False},
        "gateway": {"standalone": True},
        "memory": {"memory_enabled": True, "user_profile_enabled": True, "write_approval": False},
        "skills": {"write_approval": False},
        "auxiliary": {"background_review": {"enabled": True, "provider": "auto", "model": ""}},
        "display": {"memory_notifications": "on"},
        "plugins": {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}},
        }},
    }
    if custom:
        config["providers"] = {provider.split(":", 1)[1]: {"base_url": base_url.rstrip("/"), "api_mode": api_mode}}
        if session_affinity_header:
            config["providers"][provider.split(":", 1)[1]]["session_affinity_header"] = session_affinity_header
    elif base_url:
        config["model"]["base_url"] = base_url.rstrip("/")
    return config
