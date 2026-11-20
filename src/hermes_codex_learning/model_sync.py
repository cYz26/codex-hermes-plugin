"""Synchronize Hermes learning model selection and fallback with devbox Codex."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from .config import Settings
from .enterprise import refresh_catalog_model_boundary
from .review_retries import is_model_unavailable_error

ARK_ALIASES = {
    "gemini-4-carbon": "ark:gemini-4-carbon",
    "ark:gemini-4-carbon": "ark:gemini-4-carbon",
    "gemini-4-neon": "ark:gemini-4-neon",
    "ark:gemini-4-neon": "ark:gemini-4-neon",
    "gpt-6-astra": "ark:gpt-6-astra",
    "ark:gpt-6-astra": "ark:gpt-6-astra",
    "gpt-6-1-sol": "ark:gpt-6-1-sol",
    "GPT-6.1-Sol": "ark:gpt-6-1-sol",
    "ark:gpt-6-1-sol": "ark:gpt-6-1-sol",
    "gpt-6-2-sol": "ark:gpt-6-2-sol",
    "GPT-6.2-Sol": "ark:gpt-6-2-sol",
    "ark:gpt-6-2-sol": "ark:gpt-6-2-sol",
    "kimi-k3-1-pro-highspeed": "ark:kimi-k3-1-pro-highspeed",
    "ark:kimi-k3-1-pro-highspeed": "ark:kimi-k3-1-pro-highspeed",
}
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_COOLDOWN_SECONDS = 300.0
INCOMPATIBLE_COOLDOWN_SECONDS = 3600.0


def _cap_effort(effort: str | None, cap: str | None) -> str | None:
    if not cap:
        return effort
    cap_norm = cap.strip().lower()
    if not effort:
        return cap_norm
    eff_norm = effort.strip().lower()
    if eff_norm in EFFORT_ORDER and cap_norm in EFFORT_ORDER:
        return cap_norm if EFFORT_ORDER.index(eff_norm) > EFFORT_ORDER.index(cap_norm) else eff_norm
    return cap_norm


def codex_home_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def read_codex_catalog(codex_home: Path | None = None) -> dict:
    home = codex_home_dir(codex_home)
    cfg_path = home / "config.toml"
    if not cfg_path.is_file() or cfg_path.is_symlink():
        return {"available": False, "codex_home": str(home), "reason": "codex_config_missing"}
    try:
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "codex_home": str(home), "reason": "codex_config_invalid"}
    provider_name = cfg.get("model_provider")
    providers = cfg.get("model_providers") or {}
    provider_block = providers.get(provider_name) if isinstance(providers, dict) and isinstance(provider_name, str) else {}
    if not isinstance(provider_block, dict):
        provider_block = {}
    base_url = str(provider_block.get("base_url") or "").rstrip("/")
    catalog_path_raw = cfg.get("model_catalog_json")
    models: dict[str, dict] = {}
    catalog_order: list[str] = []
    if isinstance(catalog_path_raw, str) and catalog_path_raw:
        cat_path = Path(catalog_path_raw).expanduser()
        if cat_path.is_file() and not cat_path.is_symlink():
            try:
                cat_data = json.loads(cat_path.read_text(encoding="utf-8"))
                for item in cat_data.get("models") or []:
                    slug = item.get("slug")
                    if not isinstance(slug, str) or not slug:
                        continue
                    levels = [
                        lvl.get("effort") if isinstance(lvl, dict) else lvl
                        for lvl in (item.get("supported_reasoning_levels") or [])
                    ]
                    levels = [lvl for lvl in levels if isinstance(lvl, str) and lvl]
                    models[slug] = {"slug": slug, "efforts": levels or ["low", "medium", "high", "max"]}
                    catalog_order.append(slug)
            except (OSError, ValueError):
                pass
    fallback_order: list[str] = []
    cooldown_seconds = DEFAULT_COOLDOWN_SECONDS
    router_cfg_path = home / "ark-router-config.json"
    if provider_name == "ark" and router_cfg_path.is_file() and not router_cfg_path.is_symlink():
        try:
            rcfg = json.loads(router_cfg_path.read_text(encoding="utf-8"))
            if isinstance(rcfg.get("fallback_cooldown_seconds"), (int, float)):
                cooldown_seconds = float(rcfg["fallback_cooldown_seconds"])
            for m in rcfg.get("model_fallback_order") or []:
                if isinstance(m, str) and m in models and m not in fallback_order:
                    fallback_order.append(m)
        except (OSError, ValueError):
            pass
    for slug in catalog_order:
        if slug not in fallback_order:
            fallback_order.append(slug)
    return {
        "available": bool(models and base_url),
        "codex_home": str(home),
        "config_path": str(cfg_path),
        "config_mtime": cfg_path.stat().st_mtime,
        "provider": provider_name,
        "base_url": base_url,
        "model": cfg.get("model"),
        "review_model": cfg.get("review_model") or cfg.get("model"),
        "reasoning_effort": cfg.get("model_reasoning_effort") or "max",
        "models": models,
        "catalog_order": catalog_order,
        "fallback_order": fallback_order,
        "cooldown_seconds": max(30.0, min(cooldown_seconds, 3600.0)),
    }


def normalize_model_name(raw_model: str | None, codex_info: dict) -> str | None:
    if not isinstance(raw_model, str) or not raw_model.strip():
        return None
    candidate = raw_model.strip()
    models = codex_info.get("models") or {}
    if candidate in models:
        return candidate
    if codex_info.get("provider") == "ark":
        mapped = ARK_ALIASES.get(candidate) or ARK_ALIASES.get(candidate.lower())
        if mapped in models:
            return mapped
        prefixed = "ark:" + candidate.lower().replace(".", "-")
        if prefixed in models:
            return prefixed
    for slug in models:
        if slug.lower() == candidate.lower():
            return slug
    return None


def clamp_effort(requested_effort: str | None, model_slug: str, codex_info: dict) -> str:
    spec = (codex_info.get("models") or {}).get(model_slug) or {}
    supported = spec.get("efforts") or ["low", "medium", "high", "max"]
    effort = (requested_effort or "max").strip().lower()
    if effort in supported:
        return effort
    if effort in EFFORT_ORDER:
        idx = EFFORT_ORDER.index(effort)
        for lower in reversed(EFFORT_ORDER[:idx]):
            if lower in supported:
                return lower
    return supported[-1]


def _read_pilot_config(settings: Settings) -> tuple[bytes, dict]:
    config_path = settings.home / "config.yaml"
    if not config_path.is_file() or config_path.is_symlink():
        raise FileNotFoundError(f"Pilot config.yaml not found at {config_path}")
    raw_bytes = config_path.read_bytes()
    try:
        import sys
        added = False
        if str(settings.source) not in sys.path and settings.source.is_dir():
            sys.path.insert(0, str(settings.source))
            added = True
        try:
            import hermes_yaml as yaml
            parsed = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
        finally:
            if added:
                try:
                    sys.path.remove(str(settings.source))
                except ValueError:
                    pass
    except ImportError:
        proc = subprocess.run(
            [str(settings.interpreter), "-I", "-B", "-c",
             "import json, pathlib, sys; sys.path.insert(0, sys.argv[1]); import hermes_yaml as y; "
             "print(json.dumps(y.safe_load(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')) or {}))",
             str(settings.source), str(config_path)],
            capture_output=True, text=True, check=True, timeout=15,
        )
        parsed = json.loads(proc.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("Pilot config.yaml root must be a mapping")
    return raw_bytes, parsed


def _write_pilot_config(settings: Settings, updated: dict) -> None:
    config_path = settings.home / "config.yaml"
    try:
        import sys
        added = False
        if str(settings.source) not in sys.path and settings.source.is_dir():
            sys.path.insert(0, str(settings.source))
            added = True
        try:
            from hermes_cli.config import atomic_config_write
            atomic_config_write(config_path, updated)
            return
        finally:
            if added:
                try:
                    sys.path.remove(str(settings.source))
                except ValueError:
                    pass
    except ImportError:
        proc = subprocess.run(
            [str(settings.interpreter), "-I", "-B", "-c",
             "import json, pathlib, sys; sys.path.insert(0, sys.argv[1]); "
             "from hermes_cli.config import atomic_config_write; "
             "atomic_config_write(pathlib.Path(sys.argv[2]), json.loads(sys.stdin.read()))",
             str(settings.source), str(config_path)],
            input=json.dumps(updated, ensure_ascii=False),
            capture_output=True, text=True, check=True, timeout=15,
        )


def _pilot_base_url(pilot_doc: dict) -> str:
    model_cfg = pilot_doc.get("model") or {}
    provider = str(model_cfg.get("provider") or "")
    if provider.startswith("custom:"):
        name = provider.split(":", 1)[1]
        pblock = (pilot_doc.get("providers") or {}).get(name) or {}
        return str(pblock.get("base_url") or "").rstrip("/")
    return str(model_cfg.get("base_url") or "").rstrip("/")


def probe_review_model(settings: Settings, target_model: str, effort: str, *, timeout: int = 30) -> dict:
    """Verify Responses protocol + Hermes review tool schemas before activating a model."""
    script = (
        "import json, os, sys, urllib.request, urllib.error\n"
        "os.environ['HERMES_HOME'] = sys.argv[2]\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from hermes_cli.config import load_config_readonly\n"
        "from hermes_cli.runtime_provider import resolve_runtime_provider\n"
        "import model_tools\n"
        "cfg = load_config_readonly() or {}\n"
        "prov = (cfg.get('model') or {}).get('provider')\n"
        "rt = resolve_runtime_provider(requested=prov, target_model=sys.argv[3])\n"
        "defs = model_tools.get_tool_definitions(enabled_toolsets=['memory', 'skills'])\n"
        "tools = []\n"
        "for d in defs:\n"
        "    fn = d.get('function', d)\n"
        "    if fn.get('name') in {'memory', 'skill_manage', 'skill_view', 'skills_list'}:\n"
        "        tools.append({'type': 'function', 'name': fn['name'], 'description': fn.get('description', ''), 'parameters': fn.get('parameters', {})})\n"
        "url = rt['base_url'].rstrip('/') + '/responses'\n"
        "headers = {'Content-Type': 'application/json', 'Authorization': f\"Bearer {rt['api_key']}\", 'x-session-id': 'hermes-model-gate'}\n"
        "body = {\n"
        "    'model': sys.argv[3],\n"
        "    'instructions': 'Do not call tools. Reply READY.',\n"
        "    'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Reply READY.'}]}],\n"
        "    'tools': tools,\n"
        "    'reasoning': {'effort': sys.argv[4]},\n"
        "    'stream': True,\n"
        "}\n"
        "req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=int(sys.argv[5])) as resp:\n"
        "        raw = resp.read().decode('utf-8', errors='replace')\n"
        "        ok = resp.status == 200 and 'response.completed' in raw\n"
        "        print(json.dumps({'ok': ok, 'http_status': resp.status, 'tools_count': len(tools)}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    msg = e.read().decode('utf-8', errors='replace')[:400]\n"
        "    print(json.dumps({'ok': False, 'http_status': e.code, 'error': msg}))\n"
    )
    proc = subprocess.run(
        [str(settings.interpreter), "-I", "-B", "-c", script,
         str(settings.source), str(settings.home), target_model, effort, str(timeout)],
        capture_output=True, text=True, check=False, timeout=timeout + 10,
        env={**os.environ, **settings.native_environment()},
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "probe_failed")[:300]}
    return json.loads(lines[-1])


def evict_stale_runtime_agents(api, target_model: str) -> int:
    service = getattr(api, "_service", None)
    runtime = getattr(service, "runtime", None) or getattr(service, "_runtime", None)
    agents = getattr(runtime, "agents", None)
    close_scope = getattr(runtime, "close_scope", None)
    if not isinstance(agents, dict) or not callable(close_scope):
        return 0
    evicted = 0
    for handle, agent in list(agents.items()):
        if getattr(agent, "model", None) != target_model:
            try:
                close_scope(handle)
                evicted += 1
            except Exception:
                pass
    return evicted


def switch_pilot_model(settings: Settings, store, target_model_raw: str, *,
                       requested_effort: str | None = None, codex_home: Path | None = None,
                       apply: bool = False, verify_probe: bool = True,
                       pin: bool = False, reason: str = "manual_switch",
                       retry_queue=None, api=None) -> dict:
    codex_info = read_codex_catalog(codex_home)
    before_bytes, before_doc = _read_pilot_config(settings)
    current_model = str((before_doc.get("model") or {}).get("default") or "")
    current_effort = str((before_doc.get("agent") or {}).get("reasoning_effort") or "max")
    pilot_url = _pilot_base_url(before_doc)

    if codex_info.get("available") and pilot_url == codex_info["base_url"]:
        resolved_model = normalize_model_name(target_model_raw, codex_info)
        if not resolved_model:
            raise ValueError(
                f"Model {target_model_raw!r} is not in active Codex catalog ({sorted(codex_info['models'])})"
            )
        capped_effort = _cap_effort(requested_effort or current_effort, settings.review_reasoning_effort)
        effort = clamp_effort(capped_effort, resolved_model, codex_info)
        allowed_models = set(codex_info["models"])
    else:
        resolved_model = target_model_raw.strip()
        if not resolved_model:
            raise ValueError("A target model identifier is required")
        capped_effort = _cap_effort(requested_effort or current_effort or "max", settings.review_reasoning_effort)
        effort = (capped_effort or "max").strip().lower()
        allowed_models = {resolved_model}

    changed = (resolved_model != current_model) or (effort != current_effort)
    plan = {
        "previous_model": current_model,
        "target_model": resolved_model,
        "previous_effort": current_effort,
        "target_effort": effort,
        "changed": changed,
        "reason": reason,
        "codex_catalog_aligned": bool(codex_info.get("available") and pilot_url == codex_info.get("base_url")),
        "applied": False,
    }
    if verify_probe and (apply or changed):
        probe = probe_review_model(settings, resolved_model, effort)
        plan["protocol_probe"] = probe
        if not probe.get("ok"):
            raise RuntimeError(f"Protocol gate rejected {resolved_model}: {probe.get('http_status')} {probe.get('error', '')}".strip())
    if not apply:
        return {**plan, "dry_run": True}

    now = time.time()
    backup_path = None
    boundary_refreshed = False
    if changed:
        after_doc = {
            **before_doc,
            "model": {**(before_doc.get("model") or {}), "default": resolved_model},
            "agent": {**(before_doc.get("agent") or {}), "reasoning_effort": effort},
        }
        backup_dir = settings.home / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path = backup_dir / f"config.yaml.pre-model-switch-{int(now)}"
        shutil.copy2(settings.home / "config.yaml", backup_path)
        backup_path.chmod(0o600)
        _write_pilot_config(settings, after_doc)
        boundary_refreshed = refresh_catalog_model_boundary(
            settings.data_policy, before_bytes, before_doc, after_doc, allowed_models,
        )

    unavailable = {k: v for k, v in (store.get("unavailable-models") or {}).items() if v > now and k != resolved_model}
    store.set("unavailable-models", unavailable)
    sync_state = {
        "active_model": resolved_model,
        "reasoning_effort": effort,
        "mode": "pinned" if pin else "follow_codex",
        "pinned_model": resolved_model if pin else None,
        "updated_at": now,
        "reason": reason,
    }
    store.set("model-sync-state", sync_state)
    evicted = evict_stale_runtime_agents(api, resolved_model) if api is not None else 0
    requeued = retry_queue.requeue_model_unavailable(resolved_model) if retry_queue is not None else []
    return {
        **plan,
        "applied": True,
        "backup": str(backup_path) if backup_path else None,
        "enterprise_boundary_refreshed": boundary_refreshed,
        "evicted_cached_scopes": evicted,
        "requeued_blocked_reviews": len(requeued),
        "sync_mode": sync_state["mode"],
    }


def record_observed_codex_model(store, turn_data: dict) -> None:
    if turn_data.get("source", {}).get("executor") == "Hermes":
        return
    trace = turn_data.get("host_trace") or {}
    model = trace.get("model")
    if not isinstance(model, str) or not model:
        return
    now = time.time()
    prev = store.get("codex-observed-model") or {}
    if prev.get("model") == model and prev.get("effort") == trace.get("effort") and now - prev.get("at", 0) < 30:
        return
    store.set("codex-observed-model", {
        "model": model,
        "effort": trace.get("effort"),
        "at": now,
        "chat": turn_data.get("source", {}).get("chat"),
        "turn": turn_data.get("source", {}).get("turn"),
    })


def _is_model_protocol_incompatibility(error: str | None) -> bool:
    if not error:
        return False
    text = str(error)
    return ("content_policy_blocked" in text and "Completed tool invocation from an earlier model" in text)


def _is_gemini_adapter_model(model: str | None) -> bool:
    return bool(model and "gemini" in str(model).lower())


def record_review_model_failure(bridge, error: str | None, *, failure_reason: str | None = None,
                                codex_home: Path | None = None) -> dict | None:
    """Mark an unavailable or repeatedly incompatible/timing-out model in cooldown and fail over."""
    unavailable_err = is_model_unavailable_error(error)
    incompat_err = _is_model_protocol_incompatibility(error)
    deadline_err = failure_reason == "review_deadline" or (bool(error) and "wall-clock limit" in str(error))
    if not (unavailable_err or incompat_err or deadline_err):
        return None
    if (codex_home is None and not os.environ.get("CODEX_HOME")
            and not bridge.settings.home.resolve().is_relative_to((Path.home() / ".hermes-codex-learning").resolve())):
        return None
    config_path = bridge.settings.home / "config.yaml"
    if not config_path.is_file():
        return None
    codex_info = read_codex_catalog(codex_home)
    if not codex_info.get("available"):
        return None
    try:
        _, doc = _read_pilot_config(bridge.settings)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if _pilot_base_url(doc) != codex_info["base_url"]:
        return None
    failed_model = str((doc.get("model") or {}).get("default") or "")
    now = time.time()
    if deadline_err and not (unavailable_err or incompat_err):
        # Require two consecutive review_deadline timeouts within 2 hours before cooling down a model.
        streaks = bridge.store.get("model-deadline-streaks") or {}
        prev = streaks.get(failed_model) or {}
        count = (int(prev.get("count", 0)) + 1) if now - float(prev.get("at", 0)) < 7200 else 1
        streaks[failed_model] = {"count": count, "at": now}
        bridge.store.set("model-deadline-streaks", streaks)
        if count < 2:
            return {"failed_model": failed_model, "switched": False, "reason": "deadline_streak_below_threshold"}
    elif failed_model:
        streaks = bridge.store.get("model-deadline-streaks") or {}
        if failed_model in streaks:
            streaks.pop(failed_model, None)
            bridge.store.set("model-deadline-streaks", streaks)
    unavailable = {k: v for k, v in (bridge.store.get("unavailable-models") or {}).items() if v > now}
    if failed_model:
        cooldown = max(codex_info["cooldown_seconds"], INCOMPATIBLE_COOLDOWN_SECONDS) if (incompat_err or deadline_err) else codex_info["cooldown_seconds"]
        unavailable[failed_model] = now + cooldown
    bridge.store.set("unavailable-models", unavailable)
    sync_state = bridge.store.get("model-sync-state") or {}
    explicit_fallbacks = list(bridge.settings.review_fallback_models)
    if sync_state.get("mode") == "pinned" and not explicit_fallbacks:
        return {"failed_model": failed_model, "switched": False, "reason": "model_pinned"}

    candidates = []
    if explicit_fallbacks:
        raw_list = explicit_fallbacks
    else:
        observed = (bridge.store.get("codex-observed-model") or {}).get("model")
        raw_list = [observed, codex_info.get("review_model"), codex_info.get("model"), *codex_info.get("fallback_order", [])]
    for raw in raw_list:
        norm = normalize_model_name(raw, codex_info)
        if not norm or norm in unavailable or norm == failed_model or norm in candidates:
            continue
        # Keep background fallback within the same protocol family; never auto-failover
        # an observed tool-history session onto the Gemini rewriting proxy unless explicitly listed.
        if not explicit_fallbacks and not _is_gemini_adapter_model(failed_model) and _is_gemini_adapter_model(norm):
            continue
        if norm:
            candidates.append(norm)
    for candidate in candidates:
        try:
            return switch_pilot_model(
                bridge.settings, bridge.store, candidate,
                codex_home=codex_home, apply=True, verify_probe=True,
                pin=not bridge.settings.model_sync_enabled,
                reason=f"auto_fallback_from_{failed_model}",
                retry_queue=bridge.retry_queue, api=bridge.api,
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            unavailable[candidate] = now + codex_info["cooldown_seconds"]
            bridge.store.set("unavailable-models", unavailable)
    return {"failed_model": failed_model, "switched": False, "reason": "no_healthy_fallback"}


def sync_with_codex(bridge, *, codex_home: Path | None = None) -> dict | None:
    """Keep pilot's review model aligned with devbox Codex when sharing the same local router."""
    if not bridge.settings.model_sync_enabled:
        return None
    now = time.monotonic()
    if now - getattr(bridge, "_last_model_sync_check", 0.0) < 15.0:
        return None
    bridge._last_model_sync_check = now
    if (codex_home is None and not os.environ.get("CODEX_HOME")
            and not bridge.settings.home.resolve().is_relative_to((Path.home() / ".hermes-codex-learning").resolve())):
        return None
    config_path = bridge.settings.home / "config.yaml"
    if not config_path.is_file():
        return None
    sync_state = bridge.store.get("model-sync-state") or {}
    if sync_state.get("mode") == "pinned":
        return None
    codex_info = read_codex_catalog(codex_home)
    if not codex_info.get("available"):
        return None
    try:
        _, doc = _read_pilot_config(bridge.settings)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if _pilot_base_url(doc) != codex_info["base_url"]:
        return None
    current_model = str((doc.get("model") or {}).get("default") or "")
    wall_now = time.time()
    unavailable = {k: v for k, v in (bridge.store.get("unavailable-models") or {}).items() if v > wall_now}

    observed = bridge.store.get("codex-observed-model") or {}
    obs_model = normalize_model_name(observed.get("model"), codex_info)
    obs_at = float(observed.get("at") or 0.0)
    cfg_model = normalize_model_name(codex_info.get("review_model") or codex_info.get("model"), codex_info)
    cfg_at = float(codex_info.get("config_mtime") or 0.0)
    manual_at = float(sync_state.get("updated_at") or 0.0)

    desired = None
    desired_effort = None
    candidates_by_recency = sorted(
        [
            (obs_at, obs_model, observed.get("effort"), "codex_session_turn_context"),
            (cfg_at, cfg_model, codex_info.get("reasoning_effort"), "codex_config_toml"),
            (manual_at, sync_state.get("active_model"), sync_state.get("reasoning_effort"), "manual_sync_state"),
        ],
        key=lambda item: item[0],
        reverse=True,
    )
    for ts, cand, eff, src in candidates_by_recency:
        if ts > 0 and cand and cand in codex_info["models"] and cand not in unavailable:
            desired, desired_effort, desired_reason = cand, eff, src
            break
    else:
        return None

    current_effort = str((doc.get("agent") or {}).get("reasoning_effort") or "max")
    target_effort = clamp_effort(
        _cap_effort(desired_effort or current_effort, bridge.settings.review_reasoning_effort),
        desired, codex_info,
    )
    if desired == current_model and target_effort == current_effort:
        evict_stale_runtime_agents(bridge.api, current_model)
        return None
    try:
        return switch_pilot_model(
            bridge.settings, bridge.store, desired,
            requested_effort=target_effort, codex_home=codex_home,
            apply=True, verify_probe=False, pin=False, reason=desired_reason,
            retry_queue=bridge.retry_queue, api=bridge.api,
        )
    except (OSError, ValueError, RuntimeError):
        return None


def model_status(settings: Settings, store, *, codex_home: Path | None = None) -> dict:
    codex_info = read_codex_catalog(codex_home)
    pilot_model, pilot_provider, pilot_effort, pilot_url = None, None, None, None
    try:
        _, doc = _read_pilot_config(settings)
        pilot_model = (doc.get("model") or {}).get("default")
        pilot_provider = (doc.get("model") or {}).get("provider")
        pilot_effort = (doc.get("agent") or {}).get("reasoning_effort")
        pilot_url = _pilot_base_url(doc)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    now = time.time()
    unavailable = {k: round(v - now, 1) for k, v in (store.get("unavailable-models") or {}).items() if v > now}
    return {
        "pilot": {
            "model": pilot_model,
            "provider": pilot_provider,
            "reasoning_effort": pilot_effort,
            "shared_codex_route": bool(codex_info.get("available") and pilot_url == codex_info.get("base_url")),
        },
        "codex": {
            "available": codex_info.get("available", False),
            "provider": codex_info.get("provider"),
            "configured_model": codex_info.get("model"),
            "configured_review_model": codex_info.get("review_model"),
            "catalog_models": codex_info.get("catalog_order", []),
            "observed_session": store.get("codex-observed-model"),
        },
        "sync": store.get("model-sync-state") or {"mode": "follow_codex"},
        "unavailable_cooldown_seconds_remaining": unavailable,
    }
