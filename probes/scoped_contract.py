"""Native semantic review against a scripted loopback provider; no real model."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--workspace",type=Path,required=True)
    parser.add_argument("--enterprise",action="store_true")
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    args.source = args.source.resolve()
    args.workspace.mkdir(parents=True)
    home, project = args.workspace/"home", args.workspace/"project"
    home.mkdir()
    project.mkdir()
    sys.path[:0] = [str(ROOT/"src"),str(args.source)]
    os.environ["HERMES_HOME"] = str(home)
    os.environ["OPENAI_API_KEY"] = "scripted-loopback-fixture"
    requests = []
    marker = "SYNTHETIC_FOREIGN_MEMORY_MUST_NOT_ENTER_REVIEW"
    body = "---\nname: learned-nfkc-checks\ndescription: Normalize synthetic header identifiers.\nlearning_scope: global_candidate\n---\nApplicability: Unicode header validation.\nLimits: only the observed CSV contract.\nVerification: test full-width and ASCII duplicate identifiers.\n"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "messages" not in request:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            requests.append(request)
            if len(requests) == 1:
                operations = [
                    ("skill_view",{"name":"private-fixture"}),
                    ("memory",{"action":"add","target":"memory","content":"Synthetic CSV header matching uses NFKC normalization."}),
                    ("skill_manage",{"operations":[{"action":"create","name":"learned-nfkc-checks","content":body}]}),
                ]
                calls = [{"index":i,"id":"fixture-"+str(i),"type":"function",
                          "function":{"name":name,"arguments":json.dumps(arguments)}} for i,(name,arguments) in enumerate(operations)]
                delta, finish = {"role":"assistant","tool_calls":calls}, "tool_calls"
            else:
                delta, finish = {"role":"assistant","content":"Saved the supported synthetic lesson."}, "stop"
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            for part in [{"choices":[{"index":0,"delta":delta,"finish_reason":None}]},
                         {"choices":[{"index":0,"delta":{},"finish_reason":finish}],
                          "usage":{"prompt_tokens":100,"completion_tokens":25,"total_tokens":125}}]:
                self.wfile.write(("data: "+json.dumps({"id":"fixture","model":"fixture",**part})+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write
    atomic_config_write(home/"config.yaml",{
        "_config_version":DEFAULT_CONFIG["_config_version"],
        "model":{"provider":"custom","default":"gpt-4.1","base_url":f"http://127.0.0.1:{server.server_port}/v1"},
        "agent":{"reasoning_effort":"max"},
        "memory":{"memory_enabled":True,"user_profile_enabled":True},
        "auxiliary":{"background_review":{"enabled":True,"provider":"auto","model":""}},
        "plugins":{"isolation":"in_process","enabled":["codex-learning-bridge"],
                   "entries":{"codex-learning-bridge":{"enabled":True,"settings":{"learning":{"enabled":True}}}}},
    })
    (home/"memories").mkdir()
    (home/"memories/MEMORY.md").write_text(marker)
    private = home/"skills/private-fixture/SKILL.md"
    private.parent.mkdir(parents=True)
    private.write_text("---\nname: private-fixture\ndescription: "+marker+"\n---\n"+marker)
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.daemon import Bridge
    from hermes_codex_learning.capture import hook
    from hermes_codex_learning.knowledge import reconcile
    from hermes_codex_learning.semantic_review import install_semantic_policy
    from hermes_codex_learning.review_input import install_review_policy
    settings = Settings(home,project,args.source,Path(sys.executable),capture_scope="global",
                        data_policy={"enabled":True,"synthetic_roots":[str(project)]},idle_seconds=0,idle_interval=0,
                        checked_publication=True,quality_checks=True)
    if args.enterprise:
        from dataclasses import replace
        from hermes_codex_learning.enterprise import make_boundary
        from hermes_codex_learning.store import atomic_json
        policy_path=home/"enterprise-boundary.json"
        atomic_json(policy_path,make_boundary(settings))
        settings=replace(settings,data_policy={"enabled":True,"mode":"trusted_enterprise","enterprise_boundary":str(policy_path)})
    ctx = PluginContext(PluginManifest(name="codex-learning-bridge",source="user"),get_plugin_manager())
    api = ctx.learning(version=1)
    guard = install_semantic_policy(api,settings,ctx)
    install_review_policy(api)
    bridge = Bridge(settings,api)
    try:
        bridge.store.set("knowledge-index-v1",reconcile({},api.read(kind="context"),bootstrap_project="/unrelated"))
        for event in [{"hook_event_name":"UserPromptSubmit","prompt":"Synthetic fixture: normalize CSV headers to NFKC; retain the supported project convention."},
                      {"hook_event_name":"Stop","last_assistant_message":"Verified synthetic full-width and ASCII matching."}]:
            hook(dict(event,cwd=str(project),session_id="fixture-chat",turn_id="fixture-turn"),settings)
        bridge.ingest()
        bridge.idle_reviews()
        bridge.reviews()
        bridge.publish()
        context = bridge.rpc("context",{"cwd":str(project),"query":"header normalization"})
        result = {
            "scope":"native scripted provider; not real model or App",
            "requests":len(requests),
            "foreign_context_withheld":bool(requests) and all(marker not in json.dumps(value) for value in requests),
            "foreign_catalog_withheld":bool(requests) and "private-fixture" not in json.dumps(requests[0]),
            "memory_added":"NFKC normalization" in json.dumps(context["memories"]),
            "foreign_memory_preserved":marker in (home/"memories/MEMORY.md").read_text(),
            "foreign_memory_not_recalled":marker not in json.dumps(context),
            "skill_created":(home/"skills/learned-nfkc-checks/SKILL.md").is_file(),
        }
        if args.enterprise:
            other=bridge.rpc("context",{"cwd":str(args.workspace/"other"),"query":"header normalization"})
            result["enterprise_shared"]=any(x.get("name")=="learned-nfkc-checks" and x.get("audience")=="enterprise" for x in other["skills"])
            result["other_project_memory_empty"]=not other["memories"]
            with (home/"config.yaml").open("a") as stream:
                stream.write("\n# model configuration changed\n")
            revoked=bridge.rpc("context",{"cwd":str(project),"query":"header normalization"})
            result["revoked_enterprise_memory_hidden"]=not revoked["memories"]
            result["revoked_enterprise_skill_hidden"]=not any(x["name"]=="learned-nfkc-checks" for x in revoked["skills"])
        result["passed"] = result["requests"] >= 2 and all(value for key,value in result.items() if key not in {"scope","requests"})
        print(json.dumps(result))
        return 0 if result["passed"] else 1
    finally:
        bridge.store.close()
        guard.dispose()
        api.close()
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
