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
    parser.add_argument("--explicit",action="store_true", help="Exercise explicit requests and two material batches with automatic reviews disabled")
    parser.add_argument("--live-settings",type=Path, help="Opt-in synthetic acceptance using only the existing model/provider routing; never App evidence")
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    args.source = args.source.resolve()
    args.workspace.mkdir(parents=True)
    home, project = args.workspace/"home", args.workspace/"project"
    home.mkdir()
    project.mkdir()
    sys.path[:0] = [str(ROOT/"src"),str(args.source)]
    os.environ["HERMES_HOME"] = str(home)
    if not args.live_settings:
        os.environ["OPENAI_API_KEY"] = "scripted-loopback-fixture"
    requests = []
    rounds, references = {}, []
    marker = "SYNTHETIC_FOREIGN_MEMORY_MUST_NOT_ENTER_REVIEW"
    body = "---\nname: learned-nfkc-checks\ndescription: Normalize synthetic header identifiers.\nlearning_scope: global_candidate\n---\nApplicability: Unicode header validation.\nLimits: only the observed CSV contract.\nVerification: test full-width and ASCII duplicate identifiers.\n"
    material_skill = "learned-source-rules"
    def supported_write(tool_name, arguments, request):
        if tool_name not in {"memory","skill_manage"}:
            return arguments
        import re
        sources=[]
        for message in request.get("messages",[]):
            content=message.get("content") or ""
            if not isinstance(content,str):
                continue
            for match in re.finditer(r"\[Evidence (\{[^\n]+\})\]\n",content):
                label=json.loads(match[1])
                if label.get("scope")=="own":
                    sources.append((label,content[match.end():]))
        basis=[]
        for label,content in sources:
            if label.get("kind") == "material":
                quote=next((text for text in ("Synthetic source rule one","Synthetic source rule two","fixture://sources/one","fixture://sources/two") if text in content),content[:80])
            elif label.get("kind") == "request":
                quote="normalize CSV headers to NFKC"
            elif label.get("kind") == "result":
                quote="Verified synthetic full-width and ASCII matching."
            else:
                continue
            if quote in content:
                basis.append({"ref":label["ref"],"quote":quote})
        material=any(label.get("kind")=="material" for label,_ in sources)
        proof={"classification":"project_fact" if tool_name=="memory" else "procedure","basis":basis[:6],
               "rationale":"The admitted synthetic source rule belongs in the named skill's reference index and must preserve its source scope." if material else
                           "CSV header normalization to NFKC and full-width matching are the supported synthetic project convention and reusable procedure."}
        if "operations" in arguments:
            return {"operations":[{**operation,"evidence":proof} for operation in arguments["operations"]]}
        return {**arguments,"evidence":proof}
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
            serialized = json.dumps(request, ensure_ascii=False)
            phase = "reconcile" if "All material batches finished" in serialized else (
                "batch1" if "Batch 1/2" in serialized else "batch2" if "Batch 2/2" in serialized else "initial")
            turn = rounds.get(phase, 0)
            rounds[phase] = turn + 1
            if phase != "initial" and turn < (3 if phase == "batch1" else 2):
                import re
                if phase == "batch1" and turn == 0:
                    operations = [("skill_manage", {"operations": [{
                        "action": "create", "name": material_skill,
                        "content": body.replace("learned-nfkc-checks", material_skill),
                    }]})]
                elif turn == (1 if phase == "batch1" else 0):
                    operations = [("skill_view", {"name": material_skill})]
                else:
                    operations = []
                    if phase != "reconcile":
                        path = re.search(r"distill this topic into (references/[a-f0-9]{16}\.md)", serialized).group(1)
                        if path not in references:
                            references.append(path)
                        operations.append({"action":"write_file","name":material_skill, "file_path":path,
                                           "file_content":"Synthetic source rule, with applicability and verification.\n"})
                    content = body.replace("learned-nfkc-checks", material_skill) + "\n" + "\n".join(
                        f"Load [{path}]({path}) for the matching source topic." for path in references)
                    operations.append({"action":"patch", "name":material_skill, "content":content})
                    operations = [("skill_manage", {"operations":operations})]
                calls = [{"index":i,"id":phase+"-"+str(turn)+"-"+str(i),"type":"function",
                          "function":{"name":name,"arguments":json.dumps(supported_write(name,arguments,request))}} for i,(name,arguments) in enumerate(operations)]
                delta, finish = {"role":"assistant","tool_calls":calls}, "tool_calls"
            elif len(requests) == 1:
                operations = [
                    ("skill_view",{"name":"private-fixture"}),
                    ("memory",{"action":"add","target":"memory","content":"Synthetic CSV header matching uses NFKC normalization."}),
                    ("skill_manage",{"operations":[{"action":"create","name":"learned-nfkc-checks","content":body}]}),
                ]
                calls = [{"index":i,"id":"fixture-"+str(i),"type":"function",
                          "function":{"name":name,"arguments":json.dumps(supported_write(name,arguments,request))}} for i,(name,arguments) in enumerate(operations)]
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
    configuration = {
        "_config_version":DEFAULT_CONFIG["_config_version"],
        "model":{"provider":"custom","default":"gpt-4.1","base_url":f"http://127.0.0.1:{server.server_port}/v1"},
        "agent":{"reasoning_effort":"max"},
        "memory":{"memory_enabled":True,"user_profile_enabled":True},
        "auxiliary":{"background_review":{"enabled":not args.explicit,"provider":"auto","model":""},
                     "title_generation":{"enabled":False}},
        "plugins":{"isolation":"in_process","enabled":["codex-learning-bridge"],
                   "entries":{"codex-learning-bridge":{"enabled":True,"settings":{"learning":{"enabled":True}}}}},
    }
    if args.live_settings:
        from hermes_codex_learning.config import Settings
        from ruamel.yaml import YAML
        live = Settings.load(args.live_settings)
        live_config = YAML(typ="safe").load((live.home/"config.yaml").read_text())
        model_keys = {"provider","default","api_mode","base_url","max_tokens","context_length"}
        provider_keys = {"base_url","api_mode","session_affinity_header","max_tokens","transport"}
        configuration["model"] = {k:v for k,v in live_config["model"].items() if k in model_keys}
        configuration["providers"] = {name:{k:v for k,v in entry.items() if k in provider_keys}
            for name,entry in live_config.get("providers",{}).items()}
        configuration["agent"] = {k:v for k,v in live_config.get("agent",{}).items() if k in {"reasoning_effort","max_tokens"}}
        os.environ.update(live.network_environment)
    atomic_config_write(home/"config.yaml",configuration)
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
    audit = None
    if args.live_settings:
        def observe_request(**event):
            requests.append({"messages":event.get("request_messages",[]), "system_prompt":event.get("system_prompt", "")})
        audit = ctx.register_hook("pre_api_request", observe_request)
    api = ctx.learning(version=1)
    guard = install_semantic_policy(api,settings,ctx)
    install_review_policy(api,settings)
    bridge = Bridge(settings,api)
    try:
        bridge.store.set("knowledge-index-v1",reconcile({},api.read(kind="context"),bootstrap_project="/unrelated"))
        for event in [{"hook_event_name":"UserPromptSubmit","prompt":"Synthetic fixture: normalize CSV headers to NFKC; retain the supported project convention."},
                      {"hook_event_name":"Stop","last_assistant_message":"Verified synthetic full-width and ASCII matching."}]:
            hook(dict(event,cwd=str(project),session_id="fixture-chat",turn_id="fixture-turn"),settings)
        bridge.ingest()
        if args.explicit:
            from hermes_codex_learning.learning_dispatch import prepare
            request = {"cwd":str(project),"request_id":"explicit-refine","chat_id":"fixture-chat",
                       "focus":"Retain the NFKC normalization fact and the learned-nfkc-checks skill."}
            bridge.rpc("request_learning",request)
            prepare(bridge)
        else:
            bridge.idle_reviews()
        bridge.reviews()
        bridge.publish()
        summary_review_requests = len(requests)
        summary_reviews = [json.loads(row[0]) for row in bridge.store.db.execute("SELECT payload FROM notices")
                           if json.loads(row[0]).get("kind") == "review.finished"]
        if args.explicit:
            refine = bridge.rpc("learning_request_status", {"cwd":str(project),"request_id":"explicit-refine"})
            request = {"cwd":str(project),"request_id":"explicit-materials","chat_id":"fixture-chat",
                       "mode":"learn","skill_name":material_skill,"focus":"Preserve both synthetic source rules."}
            bridge.rpc("request_learning",request)
            for chunk in ("one","two"):
                bridge.rpc("add_learning_material", {"cwd":str(project),"request_id":"explicit-materials",
                    "chunk_id":chunk,"source":"fixture://sources/"+chunk,"content":"Synthetic source rule "+chunk})
            bridge.rpc("start_learning", {"cwd":str(project),"request_id":"explicit-materials"})
            for _ in range(3):
                prepare(bridge)
                bridge.reviews()
                bridge.last_publish = 0
                bridge.publish()
            learned = bridge.rpc("learning_request_status", {"cwd":str(project),"request_id":"explicit-materials"})
        context = bridge.rpc("context",{"cwd":str(project),"query":"header normalization"})
        summary = "Saved the supported synthetic lesson."
        receipt = summary_reviews[0].get("receipt", "") if len(summary_reviews) == 1 else ""
        result = {
            "scope":"native real provider with synthetic fixture; not App" if args.live_settings else "native scripted provider; not real model or App",
            "requests":len(requests),
            "summary_review_requests":summary_review_requests,
            "foreign_context_withheld":bool(requests) and all(marker not in json.dumps(value) for value in requests),
            "foreign_catalog_withheld":bool(requests) and "private-fixture" not in json.dumps(requests[0]),
            "memory_added":"NFKC normalization" in json.dumps(context["memories"]),
            "foreign_memory_preserved":marker in (home/"memories/MEMORY.md").read_text(),
            "foreign_memory_not_recalled":marker not in json.dumps(context),
            "skill_created":(home/"skills/learned-nfkc-checks/SKILL.md").is_file(),
            "review_summary_reused":len(summary_reviews) == 1 and summary_reviews[0].get("review_summary") == summary,
            "review_summary_precedes_changes":("复盘简述\n" + summary in receipt
                and "修改明细" in receipt and receipt.index("复盘简述") < receipt.index("修改明细")),
            "review_status_separate":"复盘：完成" in receipt and "后续任务验证：尚未确认" in receipt,
            "summary_prompt_in_same_review":"2–3 sentences" in json.dumps(requests[:summary_review_requests],ensure_ascii=False),
            "no_extra_summary_requests":summary_review_requests == 2,
            "review_summary_not_in_business_recall":summary not in json.dumps(context),
            "review_summary_not_in_native_status":summary not in json.dumps(api.read(kind="status")),
        }
        if args.explicit:
            from tools import skill_usage
            excluded_until_adoption = not skill_usage.is_curator_managed(material_skill)
            adopted, _ = skill_usage.adopt_skill(material_skill)
            result.update(explicit_refine_completed=refine["state"]=="completed",
                          manual_focus_delivered="Retain the NFKC normalization fact" in json.dumps(requests[0]),
                          material_learning_completed=learned["state"]=="completed",
                          reference_index_verified=learned.get("reference_check",{}).get("passed",False),
                          three_batches_completed=len(learned["results"])==3,
                          material_skill_user_managed=excluded_until_adoption and any(
                              row.get("curator_management") == "user_taught" for row in learned["results"]),
                          explicit_curator_adoption=adopted and skill_usage.is_curator_managed(material_skill),
                          refine_skill_curator_managed=skill_usage.is_curator_managed("learned-nfkc-checks"))
            if not args.live_settings:
                material_requests = requests[summary_review_requests:]
                result["material_scope_matches_tool_schema"] = bool(material_requests) and all(
                    {tool.get("function", {}).get("name") for tool in request.get("tools", [])}
                    == {"skill_manage", "skill_view"} for request in material_requests)
                result["material_scope_excludes_memory_write_prompt"] = all(
                    "Use native memory to add" not in str(message.get("content", ""))
                    for request in material_requests for message in request.get("messages", [])
                    if message.get("role") == "system")
            if learned["state"] != "completed":
                (args.workspace/"request-result.json").write_text(json.dumps(learned,ensure_ascii=False,indent=2))
                outcomes = [m for request in requests for m in request.get("messages",[]) if m.get("role") == "tool"]
                (args.workspace/"tool-results.json").write_text(json.dumps(outcomes,ensure_ascii=False,indent=2))
        if args.enterprise:
            other=bridge.rpc("context",{"cwd":str(args.workspace/"other"),"query":"header normalization"})
            result["enterprise_shared"]=any(x.get("name")=="learned-nfkc-checks" and x.get("audience")=="enterprise" for x in other["skills"])
            result["other_project_memory_empty"]=not other["memories"]
            with (home/"config.yaml").open("a") as stream:
                stream.write("\n# model configuration changed\n")
            revoked=bridge.rpc("context",{"cwd":str(project),"query":"header normalization"})
            result["revoked_enterprise_memory_hidden"]=not revoked["memories"]
            result["revoked_enterprise_skill_hidden"]=not any(x["name"]=="learned-nfkc-checks" for x in revoked["skills"])
        result["passed"] = result["requests"] >= 2 and all(value for key,value in result.items()
            if key not in {"scope","requests","summary_review_requests"})
        receipt = {"summary":result,"usage":api.read(kind="status").get("usage"),"actual_app":False}
        if args.explicit:
            receipt.update(refine=refine,learn=learned)
        (args.workspace/"result.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
        print(json.dumps(result))
        return 0 if result["passed"] else 1
    finally:
        bridge.store.close()
        guard.dispose()
        if audit is not None:
            audit.dispose()
        api.close()
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
