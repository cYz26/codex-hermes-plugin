"""Compare real provider usage for the same task in native Hermes and Codex with the bridge."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from check_codex_hooks import inspect_hooks
from hermes_codex_learning import __version__
from hermes_codex_learning.authentication import bind_profile_home
from hermes_codex_learning.cli import BASE
from hermes_codex_learning.config import Settings
from hermes_codex_learning.ipc import request
from hermes_codex_learning.store import atomic_json

MODEL = "gpt-6.1-sol"
EFFORT = "max"
CASE_OK = re.compile(r"\bPAIR_CASE_OK (\d+)\b")
METRICS = ("uncached_input", "cache_read", "cache_write", "output", "reasoning", "api_calls")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(raw, *, codex=False):
    if codex:
        total, cached = raw.get("input_tokens"), raw.get("cached_input_tokens")
        assert isinstance(total, int) and isinstance(cached, int) and 0 <= cached <= total
        values = (total - cached, cached, raw.get("cache_write_input_tokens"), raw.get("output_tokens"),
                  raw.get("reasoning_output_tokens", raw.get("reasoning_tokens")), raw.get("api_calls"))
    else:
        values = tuple(raw.get(name) for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens",
                                                 "output_tokens", "reasoning_tokens", "api_calls"))
    assert all(isinstance(value, int) and value >= 0 for value in (values[0], values[1], values[3]))
    assert all(value is None or isinstance(value, int) and value >= 0 for value in values[2:])
    return dict(zip(METRICS, values))


def sum_usage(parts):
    return {key: sum(part[key] for part in parts) if all(part[key] is not None for part in parts) else None
            for key in METRICS}


def check_outputs(outputs, cases):
    ids = [int(value) for output in outputs for value in CASE_OK.findall(str(output))]
    assert sorted(set(ids)) == list(range(1, cases + 1)), "Did not observe each case's actual tool result"
    assert sum(bool(CASE_OK.search(str(output))) for output in outputs) >= cases, "Cases were batched"


def native_worker(settings, prompt, cases, output):
    os.environ.update(settings.native_environment())
    sys.path.insert(0, str(settings.source))
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import parse_reasoning_effort
    from hermes_state import SessionDB
    import run_agent

    assert Path(run_agent.__file__).resolve().is_relative_to(settings.source)
    runtime = resolve_runtime_provider(requested="openai-codex", target_model=MODEL)
    assert runtime["provider"] == "openai-codex" and runtime["api_mode"] == "codex_responses"
    db = SessionDB()
    agent = run_agent.AIAgent(model=MODEL, provider=runtime["provider"], api_mode=runtime["api_mode"],
                              api_key=runtime["api_key"], base_url=runtime["base_url"],
                              credential_pool=runtime.get("credential_pool"),
                              request_overrides=runtime.get("request_overrides"), session_db=db,
                              platform="cli", cwd=str(settings.project), quiet_mode=True,
                              reasoning_config=parse_reasoning_effort(EFFORT),
                              enabled_toolsets=["terminal", "skills", "memory"])
    try:
        result = agent.run_conversation(prompt)
        assert result["completed"] and not result["failed"]
        assert result["final_response"].strip().strip("`") == f"PAIR_OK {cases}"
        tools = [message.get("content", "") for message in result["messages"] if message.get("role") == "tool"]
        check_outputs(tools, cases)
        for worker in threading.enumerate():
            if worker.name == "bg-review":
                worker.join(timeout=300)
                assert not worker.is_alive(), "Native background review did not finish in time"
        aux = db.auxiliary_usage_by_task(agent.session_id)
        assert bool(aux.get("background_review", {}).get("api_calls")) == (cases >= 10)
        main = normalize(result)
        auxiliary = {name: normalize(row) for name, row in aux.items()}
        atomic_json(output, {"main": main, "auxiliary": auxiliary, "total": sum_usage([main, *auxiliary.values()]),
                             "checks": cases, "model": result["model"], "provider": result["provider"],
                             "review": "native_threshold" if cases >= 10 else "none_below_threshold"})
    finally:
        agent.close()
        db.close()


def prepare(root, native_source, cases):
    native_source = native_source.resolve()
    git = ["git", "-C", str(native_source)]
    assert subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip() == BASE
    assert not subprocess.check_output([*git, "status", "--porcelain"], text=True).strip(), (
        "The native comparison requires a clean checkout of the official base"
    )
    root.mkdir(parents=True, mode=0o700)
    project = root / "task"
    project.mkdir()
    fixture = project / "check_receipt.py"
    fixture.write_text("import hashlib, json, sys\nfrom pathlib import Path\n"
                       "i = int(sys.argv[1])\nr = json.loads(Path(f'receipt-{i}.json').read_text())\n"
                       "assert r['id'] == i\nassert hashlib.sha256(r['body'].encode()).hexdigest() == r['sha256']\n"
                       "print(f'PAIR_CASE_OK {i}')\n")
    for case in range(1, cases + 1):
        body = f"isolated receipt {case}"
        atomic_json(project / f"receipt-{case}.json", {"id": case, "body": body,
                                                      "sha256": hashlib.sha256(body.encode()).hexdigest()})
    prompt = (f"Validate receipts numbered 1 through {cases} in this workspace. For each receipt, run "
              "`python3 -B check_receipt.py N` as a separate terminal tool call with its number. Wait for each "
              "tool result before calling the next one. Do not batch, run in parallel, or substitute commands. The workspace "
              f"is read-only for this task. If every check succeeds, reply exactly `PAIR_OK {cases}`.")
    atomic_json(root / "run.json", {"cases": cases, "prompt": prompt, "base": BASE, "native_source": str(native_source),
                                   "fixtures": {p.name: hash_file(p) for p in sorted(project.iterdir())}})


def lane_settings(root, base, lane, native_source):
    assert base.profile_home is not None, "Existing Hermes root login must be configured for the benchmark"
    home = root / lane
    home.mkdir(mode=0o700)
    profile = bind_profile_home(home, base.profile_home.parent / f".cost-{root.name}-{lane}")
    settings = replace(base, home=home, profile_home=profile, project=root / "task", notifications=False,
                       capture_scope="project", excluded_projects=(), source=native_source if lane == "native" else base.source)
    config = {"model": {"provider": "openai-codex", "default": MODEL, "api_mode": "codex_responses"},
              "agent": {"reasoning_effort": EFFORT}, "auth": {"adopt_external_logins": False},
              "gateway": {"standalone": True}, "curator": {"enabled": False},
              "memory": {"memory_enabled": True, "user_profile_enabled": True, "write_approval": False},
              "skills": {"write_approval": False},
              "auxiliary": {"background_review": {"enabled": True, "provider": "auto", "model": ""}}}
    if lane == "bridge":
        config["plugins"] = {"isolation": "in_process", "enabled": ["codex-learning-bridge"], "entries": {
            "codex-learning-bridge": {"enabled": True, "settings": {"learning": {"enabled": True}}},
        }}
        plugin = home / "plugins" / "codex-learning-bridge"
        plugin.parent.mkdir()
        plugin.symlink_to(ROOT / "hermes-plugin", target_is_directory=True)
    atomic_json(home / "config.yaml", config)  # JSON is valid YAML.
    path = home / "bridge.json"
    atomic_json(path, {"home": str(home), "project": str(settings.project), "source": str(settings.source),
                       "python": str(settings.python), "profile_home": str(profile), "notifications": False,
                       "network_environment": settings.network_environment, "idle_seconds": settings.idle_seconds,
                       "idle_interval": settings.idle_interval})
    return settings, path


def await_result(callback, worker, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise RuntimeError(f"Bridge worker exited with {worker.returncode}")
        try:
            value = callback()
            if value is not None:
                return value
        except (OSError, sqlite3.OperationalError):
            pass
        time.sleep(1)
    raise TimeoutError("Bridge worker did not publish the expected result")


def bridge_lane(settings, path, prompt, cases, codex):
    trust = inspect_hooks(codex, settings.project)
    assert trust["ready"], "The benchmark requires the user's trusted installed hooks"
    hook_paths = [h.get("sourcePath", "") for entry in trust["entries"] for h in entry["hooks"]]
    assert all(f"/{__version__}/" in path for path in hook_paths), "Trusted hook package is not the candidate version"
    env = {**os.environ, **settings.network_environment, "HERMES_LEARNING_CONFIG": str(path)}
    with (settings.home / "worker.log").open("wb") as log:
        worker = subprocess.Popen([str(settings.python), "-B", str(ROOT / "scripts/serve.py"), str(path)],
                                  cwd=settings.project, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            await_result(lambda: request(settings, "learning_status"), worker, 20)
            command = [codex, "exec", "-C", str(settings.project), "--skip-git-repo-check", "--ephemeral", "--json",
                       "-s", "workspace-write", "--add-dir", str(settings.home), "-m", MODEL,
                       "-c", f'model_reasoning_effort="{EFFORT}"', prompt]
            with (settings.home / "codex.jsonl").open("w") as events, (settings.home / "codex.log").open("w") as err:
                subprocess.run(command, cwd=settings.project, env=env, stdout=events, stderr=err, check=True, timeout=600)
            events = [json.loads(line) for line in (settings.home / "codex.jsonl").read_text().splitlines() if line.strip()]
            chat, = [item["thread_id"] for item in events if item["type"] == "thread.started"]
            main_raw, = [item["usage"] for item in events if item["type"] == "turn.completed"]
            messages = [item["item"]["text"] for item in events if item["type"] == "item.completed"
                        and item["item"]["type"] == "agent_message"]
            assert messages[-1].strip().strip("`") == f"PAIR_OK {cases}"
            with sqlite3.connect((settings.state / "bridge.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                hooks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE "
                         "json_extract(payload,'$.session_id')=?", (chat,))]
            assert any(row["hook_event_name"] == "UserPromptSubmit" and row.get("prompt") == prompt for row in hooks)
            assert any(row["hook_event_name"] == "Stop" for row in hooks)
            check_outputs([row.get("tool_response") for row in hooks if row["hook_event_name"] == "PostToolUse"], cases)
            reason = "native_threshold" if cases >= 10 else "idle_flush"

            def result():
                current = request(settings, "learning_status")
                if current["error"]:
                    raise RuntimeError("Isolated bridge reported an error; inspect private worker log")
                snapshot = current.get("snapshot") or {}
                intents = [item for item in snapshot.get("intents", []) if item["reason"] == reason]
                if not intents:
                    return None
                assert len(intents) == 1
                if intents[0]["status"] in {"queued", "running"}:
                    return None
                assert intents[0]["status"] == "completed", "Background review failed"
                rows = snapshot["usage"]["native_auxiliary_by_session"][intents[0]["handle"]]
                assert rows["background_review"]["api_calls"] > 0
                attempt, = snapshot["attempts"]
                assert attempt["run_status"] == "completed" and attempt["usage"]["model"] == MODEL
                assert normalize(attempt["usage"]) == normalize(rows["background_review"])
                return {name: normalize(row) for name, row in rows.items()}

            aux = await_result(result, worker, 480)
            main = normalize(main_raw, codex=True)
            return {"main": main, "auxiliary": aux, "total": sum_usage([main, *aux.values()]),
                    "checks": cases, "model": MODEL, "review": reason, "hook_events": len(hooks)}
        finally:
            if worker.poll() is None:
                try:
                    request(settings, "shutdown")
                    worker.wait(timeout=10)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    worker.terminate()
                    worker.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="Fresh short private path under /tmp")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--native-source", type=Path, default=Path.home() / ".hermes/hermes-agent")
    parser.add_argument("--cases", type=int, choices=(1, 10), default=10)
    parser.add_argument("--codex", default="/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    parser.add_argument("--step", choices=("all", "native", "bridge", "native-worker"), default="all")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    root = args.run_dir.resolve()
    assert root.is_relative_to(Path("/tmp").resolve()) and root != Path("/tmp").resolve()
    base = Settings.load(args.settings)
    if args.step == "native-worker":
        data = load(root / "run.json")
        native_worker(base, data["prompt"], data["cases"], root / "native.json")
        return
    if not root.exists():
        prepare(root, args.native_source, args.cases)
    data = load(root / "run.json")
    assert data["cases"] == args.cases
    for lane in ("native", "bridge") if args.step == "all" else (args.step,):
        assert {p.name: hash_file(p) for p in sorted((root / "task").iterdir())} == data["fixtures"]
        settings, path = lane_settings(root, base, lane, Path(data["native_source"]))
        try:
            start = time.monotonic()
            if lane == "native":
                command = [str(settings.python), "-B", str(Path(__file__).resolve()), "--step", "native-worker",
                           "--run-dir", str(root), "--settings", str(path)]
                env = {**os.environ, **settings.native_environment(), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
                with (settings.home / "native.log").open("wb") as log:
                    subprocess.run(command, cwd=settings.project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   check=True, timeout=900)
                result = load(root / "native.json")
            else:
                result = bridge_lane(settings, path, data["prompt"], args.cases, args.codex)
            result["elapsed_seconds"] = round(time.monotonic() - start, 2)
            assert {p.name: hash_file(p) for p in sorted(settings.project.iterdir())} == data["fixtures"]
            assert not (settings.home / "auth.json").exists()
            atomic_json(root / f"{lane}.json", result)
            print(json.dumps({"lane": lane, "result": result}, ensure_ascii=False), flush=True)
        finally:
            assert settings.profile_home is not None
            if settings.profile_home.is_symlink() and settings.profile_home.resolve() == settings.home:
                settings.profile_home.unlink()
    if args.out:
        native, bridge = load(root / "native.json"), load(root / "bridge.json")
        delta = {key: bridge["total"][key] - native["total"][key] if None not in (bridge["total"][key], native["total"][key]) else None
                 for key in METRICS}
        atomic_json(args.out, {"status": "passed", "version": __version__, "native_commit": BASE,
                               "workload_cases": args.cases, "prompt_sha256": hashlib.sha256(data["prompt"].encode()).hexdigest(),
                               "fixtures": data["fixtures"], "reasoning_effort_requested": EFFORT,
                               "execution_surface": "Codex CLI with real trusted hooks vs unmodified native Hermes AIAgent",
                               "native_toolsets": ["terminal", "skills", "memory"],
                               "codex_settings": "Current local host plugin configuration; execution workspace and learning home isolated",
                               "worker_entry_sha256": hash_file(ROOT / "scripts/serve.py"),
                               "actual_fee_usd": None, "native": native, "bridge": bridge, "delta_bridge_minus_native": delta})


if __name__ == "__main__":
    main()
