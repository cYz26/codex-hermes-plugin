#!/usr/bin/env python3
"""Isolated shared owner and native CLI, with a deterministic loopback model fixture."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_codex_learning.configuration import build_native_config
from hermes_codex_learning.config import Settings
from hermes_codex_learning.ipc import request
from hermes_codex_learning.shared_cli import _bind
from hermes_codex_learning.store import atomic_json


FACT = "Native CLI fixture selected cobalt for the validation palette."
SHARED_READ = "Codex baseline receipt uses cedar."
SKILL = "learned-shared-fixture-receipt"
CODEX_SKILL = "learned-codex-baseline-fixture"
RESOURCE = "Fixture validation receipt template."
SKILL_BODY = f"""---
name: {SKILL}
description: Use when verifying a fixture receipt's version.
---

Record which command ran and verify its terminal status.
Use references/receipt.md for the receipt template.
"""


def send(handler, status, value):
    data = json.dumps(value).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class FixtureModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        send(self, 200, {"object":"list","data":[{"id":"fixture-model","object":"model"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = payload.get("messages")
        if not messages:
            send(self, 404, {"error":{"message":"The fixture provides chat completions only"}})
            return
        last = messages[-1]
        final = {"role":"assistant","content":"Fixture completed."}
        reason = "stop"
        # A successful native memory response must precede the final turn.
        if last["role"] == "user" and "fixture: write shared memory" in str(last.get("content")):
            final = {"role":"assistant", "content":None, "tool_calls":[{
                "id":"call_fixture_memory", "type":"function", "function":{
                    "name":"memory", "arguments":json.dumps({"action":"add","target":"memory","content":FACT})
                }}]}
            reason = "tool_calls"
        elif last["role"] == "user" and "fixture: check shared memory" in str(last.get("content")):
            present = SHARED_READ in "\n".join(str(row.get("content", "")) for row in messages if row["role"] == "system")
            final["content"] = "Fixture recalled cedar." if present else "Fixture did not recall cedar."
        elif last["role"] == "tool" and "call_fixture_memory" == last.get("tool_call_id"):
            result = json.loads(last["content"])
            final["content"] = "Fixture saved cobalt." if result.get("success") else "Fixture memory call did not apply."
        elif last["role"] == "user" and "fixture: write shared skill" in str(last.get("content")):
            operations = [{"action":"create", "name":SKILL, "content":SKILL_BODY},
                          {"action":"write_file", "name":SKILL, "file_path":"references/receipt.md", "file_content":RESOURCE}]
            final = {"role":"assistant", "content":None, "tool_calls":[{
                "id":"call_fixture_skill", "type":"function", "function":{
                    "name":"skill_manage", "arguments":json.dumps({"operations": operations})
                }}]}
            reason = "tool_calls"
        elif last["role"] == "tool" and "call_fixture_skill" == last.get("tool_call_id"):
            result = json.loads(last["content"])
            final["content"] = "Fixture saved the skill." if result.get("state") == "applied" else "Fixture skill call did not apply."
        elif last["role"] == "user" and "fixture: check Codex skill" in str(last.get("content")):
            final = {"role":"assistant", "content":None, "tool_calls":[{
                "id":"call_fixture_load", "type":"function", "function":{
                    "name":"skill_view", "arguments":json.dumps({"name":CODEX_SKILL})
                }}]}
            reason = "tool_calls"
        elif last["role"] == "tool" and "call_fixture_load" == last.get("tool_call_id"):
            result = json.loads(last["content"])
            seen = result.get("success") and "Check the already recorded cedar baseline" in result.get("content", "")
            final["content"] = "Fixture read the Codex skill." if seen else "Fixture skill was unavailable."
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(final)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index":i, **item} for i, item in enumerate(delta["tool_calls"])]
            for value in ({"delta":delta, "finish_reason":None}, {"delta":{}, "finish_reason":reason}):
                chunk = {"id":"fixture","object":"chat.completion.chunk","created":1,"model":"fixture-model",
                         "choices":[{"index":0,**value}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            send(self, 200, {"id":"fixture","object":"chat.completion","created":1,"model":"fixture-model",
                             "choices":[{"index":0,"message":final,"finish_reason":reason}],
                             "usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}})


@contextmanager
def fixture(args, root):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    a, b, owner = (root / name for name in ("scope-a", "scope-b", "owner"))
    profile = args.live_profile or root / "native"
    for path in (a, b, owner, profile):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (a,b):
        (path / ".git").mkdir()
    config = build_native_config(version=38, provider="custom:fixture", model="fixture-model",
                                 api_mode="chat_completions", base_url=base, reasoning_effort="low")
    config["providers"]["fixture"]["api_key"] = "fixture"
    network = {}
    if args.live_settings:
        from ruamel.yaml import YAML
        installed = Settings.load(args.live_settings)
        original = YAML(typ="safe").load((installed.home / "config.yaml").read_text())
        config["model"] = {key: value for key, value in original["model"].items()
                           if key in {"provider", "default", "api_mode", "base_url", "max_tokens", "context_length"}}
        config["providers"] = {name: {key: value for key, value in provider.items()
                                      if key in {"base_url", "api_mode", "max_tokens", "transport"}}
                               for name, provider in original.get("providers", {}).items()}
        config["agent"] = {key:value for key,value in original.get("agent",{}).items()
                           if key in {"reasoning_effort", "max_tokens"}}
        network = installed.network_environment
    config["curator"] = {"enabled": False, "consolidate":False}
    atomic_json(owner / "config.yaml", config)
    link = owner / "plugins/codex-learning-bridge"
    link.parent.mkdir()
    link.symlink_to(ROOT / "hermes-plugin", target_is_directory=True)
    execution = {**config, "plugins": {"enabled":[]}, "terminal":{"backend":"local"},
                 "display":{"streaming":False}, "update":{"auto":False}}
    atomic_json(profile / "config.yaml", execution)
    env = {key:os.environ[key] for key in ("PATH","HOME","USER","LANG","TMPDIR") if key in os.environ}
    env.update(network)
    env.update(HERMES_RUNTIME_DIR=str(root / "runtime"), HERMES_GATEWAY_LOCK_DIR=str(root / "gateway-locks"),
               PYTHONDONTWRITEBYTECODE="1")
    path = owner / "bridge.json"
    atomic_json(path, {"home":str(owner),"project":str(a),"source":str(args.source),"python":str(args.python),
                       "capture_scope":"global","checked_publication":True,"notifications":False,"updates":{"mode":"off"}})
    _bind(Settings.load(path), path, profile, apply=True)
    owner_config = Settings.load(path)
    # Seed a versioned legacy Codex project entry for the migration, not a model result.
    from hermes_codex_learning.store import Store, digest
    legacy_file = owner / "memories/MEMORY.md"
    legacy_file.parent.mkdir()
    legacy_file.write_text(SHARED_READ, encoding="utf-8")
    legacy_skill = owner / "skills" / CODEX_SKILL / "SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text(f"---\nname: {CODEX_SKILL}\ndescription: Use when reviewing a fixture baseline.\n---\n"
                            "Check the already recorded cedar baseline before comparing results.\n", encoding="utf-8")
    store = Store(owner_config.state)
    store.set("knowledge-index-v1", {"version":1,"memory":{digest(["MEMORY.md", SHARED_READ]):{"project":str(a)}},
                                     "skills":{CODEX_SKILL:{"projects":[str(a)]}}})
    store.close()

    with (root / "owner.log").open("w") as log:
        proc = subprocess.Popen([str(args.python), "-I", "-B", str(ROOT/"scripts/worker.py"), str(path)],
                                cwd=a, env=env, stdout=log, stderr=log)
        try:
            wait_for(lambda: request(owner_config, "learning_status", timeout=0.6).get("ready"), proc)
            yield owner_config, path, env, a, b, profile
        finally:
            if proc.poll() is None:
                try:
                    request(owner_config, "shutdown", timeout=2)
                    proc.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    proc.terminate()
                    proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


def wait_for(fn, proc=None, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Fixture worker exited with {proc.returncode}")
        try:
            value = fn()
            if value:
                return value
        except (OSError, RuntimeError, sqlite3.OperationalError):
            pass
        time.sleep(0.15)
    raise TimeoutError("Fixture timed out waiting for live state")


def native_cli(args, path, env, project, prompt, output):
    command = [str(args.python), "-B", str(ROOT / "scripts/bridge"), "shared-learning", "cli",
               "--settings",str(path),"--","chat","-q",prompt]
    result = subprocess.run(command, cwd=project, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120 if args.live_settings else 45)
    output.write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 0, f"Native CLI failed; inspect {output}"
    return result.stdout


def desktop_backend(args, env, project, profile, log, version):
    script = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from fastapi import HTTPException
from hermes_cli.web_models import LearningNodeEdit
from hermes_cli.web_routers.status import get_learning_graph, get_learning_node, update_learning_node
from hermes_cli.web_routers.skills import get_skill_content

async def main():
    graph = await get_learning_graph(cwd=sys.argv[2])
    target = next(n for n in graph['nodes'] if n['label'] == sys.argv[3])
    detail = await get_learning_node(target['id'], cwd=sys.argv[2])
    assert detail['content'] == target['label']
    result = await update_learning_node(LearningNodeEdit(id=target['id'], content='Fixture Journey edit.'), cwd=sys.argv[2])
    assert result['state'] == 'applied'
    try:
        await update_learning_node(LearningNodeEdit(id=target['id'], content='Must not apply.'), cwd=sys.argv[2])
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError('A stale Journey edit was accepted')
    assert (await get_skill_content(sys.argv[4]))['version'] == sys.argv[5]

asyncio.run(main())
"""
    result = subprocess.run([str(args.python), "-I", "-B", "-c", script, str(args.source), str(project), FACT,
                             SKILL, version], env={**env, "HERMES_HOME":str(profile),
                                                  "HERMES_SHARED_LEARNING_MANAGEMENT_CWD":str(project)},
                            cwd=project, capture_output=True, text=True, timeout=30)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, f"Desktop backend contract failed; inspect {log}"


def configured_model(args, env, project, profile, log):
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.config import load_config_readonly
from hermes_cli.runtime_provider import resolve_runtime_provider
from hermes_constants import parse_reasoning_effort
from hermes_state import SessionDB
from run_agent import AIAgent

cfg = load_config_readonly()
model = cfg['model']
runtime = resolve_runtime_provider(requested=model['provider'], target_model=model['default'],
                                   explicit_base_url=model.get('base_url'))
db = SessionDB()
agent = AIAgent(model=model['default'], provider=runtime.get('provider'), api_mode=runtime.get('api_mode'),
                api_key=runtime.get('api_key'), base_url=runtime.get('base_url'),
                credential_pool=runtime.get('credential_pool'), request_overrides=runtime.get('request_overrides'),
                reasoning_config=parse_reasoning_effort(cfg.get('agent', {}).get('reasoning_effort')),
                enabled_toolsets=['memory'], cwd=sys.argv[2], platform='cli', session_db=db, quiet_mode=True,
                max_iterations=4, skip_context_files=True)
try:
    result = agent.run_conversation(sys.argv[3])
    assert result.get('completed'), 'Native model conversation did not complete'
    print(result['final_response'])
finally:
    agent.close()
    db.close()
"""
    prompt = ("This is an isolated integration check with fictional data. Use the memory tool to save the following "
              "exact fact in project memory: " + FACT + " Then report the baseline material already in your "
              "project memory. If receipt is pending, say so. Do not use files or another tool.")
    result = subprocess.run([str(args.python), "-I", "-B", "-c", script, str(args.source), str(project), prompt],
                            env={**env, "HERMES_HOME":str(profile)}, cwd=project,
                            capture_output=True, text=True, timeout=120)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, f"Configured native model run failed; inspect {log}"
    return result.stdout


def run(args, root):
    with fixture(args, root) as (settings, path, env, a, b, profile):
        if args.live_settings:
            output = configured_model(args,env,a,profile,root/"provider.log")
            assert "cedar" in output.lower(), "The real model did not report the seed from shared memory"
            assert FACT in request(settings,"context",{"cwd":str(a)})["memories"]["MEMORY.md"]
            assert not request(settings,"context",{"cwd":str(b)})["memories"]
            return {"native_runtime":"passed", "codex_mcp_backend":"passed", "cross_project":"passed",
                    "model":"configured provider with synthetic input", "workspace":str(root)}
        one = native_cli(args,path,env,a,"fixture: write shared memory",root/"cli-write.log")
        assert "Fixture saved cobalt." in one, "The real CLI tool response did not confirm the shared write"
        two = native_cli(args,path,env,a,"fixture: check shared memory",root/"cli-read.log")
        assert "Fixture recalled cedar." in two, "The real CLI system prompt did not receive migrated memory"
        three = native_cli(args,path,env,b,"fixture: check shared memory",root/"cli-other.log")
        assert "Fixture did not recall cedar." in three, "Workspace received another project's memory"
        a_view = request(settings, "context", {"cwd":str(a)})
        b_view = request(settings, "context", {"cwd":str(b)})
        assert FACT in a_view["memories"]["MEMORY.md"] and not b_view["memories"]
        from hermes_codex_learning.capture import hook
        context = hook({"hook_event_name":"SessionStart", "session_id":"codex-recall-fixture", "cwd":str(a)}, settings)
        assert "Native CLI fixture selected" in context["hookSpecificOutput"]["additionalContext"]
        skill = native_cli(args,path,env,a,"fixture: write shared skill",root/"cli-skill.log")
        assert "Fixture saved the skill." in skill, "The real CLI did not write the native skill package"
        package = wait_for(lambda: request(settings,"skill_load",{"cwd":str(a),"name":SKILL,"file_path":"references/receipt.md"}))
        assert package["content"].strip() == RESOURCE and len(package["version"]) == 64
        seeded_skill = native_cli(args,path,env,a,"fixture: check Codex skill",root/"cli-skill-read.log")
        assert "Fixture read the Codex skill." in seeded_skill
        def evidence():
            status = request(settings, "learning_status", {"cwd":str(a)})["shared_learning"]
            if status["contributions"].get("Hermes",0)>=2 and any(
                    row["name"] == CODEX_SKILL and row["executor"] == "Hermes" for row in status["deliveries"]):
                return status
        latest = wait_for(evidence)
        desktop_backend(args, env, a, profile, root/"desktop-backend.log", package["version"])
        assert not (profile/"memories/MEMORY.md").exists()
        return {"native_cli":"passed", "codex_mcp_backend":"passed", "cross_project":"passed",
                "native_desktop_backend":"passed",
                "native_observations":latest["contributions"], "skill_resource_version": package["version"],
                "model":"loopback fixture", "workspace":str(root)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--python", type=lambda p: Path(p).absolute(), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live-settings", type=Path, help="Use the configured provider and synthetic data in an isolated profile")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="hsl-", dir="/tmp")).resolve()
    try:
        profile_context = nullcontext(None)
        if args.live_settings:
            selected = Settings.load(args.live_settings)
            profiles = selected.profile_home.parent if selected.profile_home else Path.home() / ".hermes/profiles"
            # A named profile can use Hermes' normal root-auth inheritance.
            # No credentials are copied out of the authorized local store.
            profile_context = tempfile.TemporaryDirectory(prefix=".shared-learning-check-", dir=profiles)
        with profile_context as native_profile:
            args.live_profile = Path(native_profile) if native_profile else None
            result = run(args, root)
        if args.output:
            atomic_json(args.output, result)
        print(json.dumps(result))
    except BaseException:
        print(f"Fixture logs: {root}", file=sys.stderr)
        raise
