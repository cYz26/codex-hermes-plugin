"""Explicit real-provider semantic contract probe, never claimed as App evidence."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings",type=Path,required=True)
    parser.add_argument("--source",type=Path)
    parser.add_argument("--workspace",type=Path,required=True)
    parser.add_argument("--enterprise",action="store_true")
    args = parser.parse_args()
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.store import atomic_json, redact
    from hermes_codex_learning.updates import catalog, verify_source
    live = Settings.load(args.settings)
    source = (args.source or live.source).resolve()
    verify_source(source,catalog()["v0.23.0"])
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True,mode=0o700)
    home, project = workspace/"home",workspace/"project"
    home.mkdir()
    project.mkdir()
    sys.path.insert(0,str(source))
    from ruamel.yaml import YAML
    from hermes_cli.config import atomic_config_write
    original = YAML(typ="safe").load((live.home/"config.yaml").read_text())
    config = {key:copy.deepcopy(original[key]) for key in
              ("_config_version","model","providers","agent","memory","skills","auxiliary","plugins","gateway","auth")
              if key in original}
    if redact(config) != config:
        raise RuntimeError("Probe requires native credential fallback; credential-bearing configuration is not copied")
    os.environ.update(live.network_environment)
    os.environ["HERMES_HOME"] = str(home)
    atomic_config_write(home/"config.yaml",config)
    plugin = home/"plugins/codex-learning-bridge"
    plugin.parent.mkdir()
    plugin.symlink_to(ROOT/"hermes-plugin",target_is_directory=True)
    marker = "SYNTHETIC_FOREIGN_MEMORY_CANARY"
    (home/"memories").mkdir()
    (home/"memories/MEMORY.md").write_text(marker)
    from hermes_codex_learning.assessment import register_source
    initial = (
        "Explicit synthetic learning fixture. Observed CSV header procedure: strip surrounding whitespace and preserve "
        "letter case. Only ASCII examples were checked. Save the observed procedure as learned-header-normalization; "
        "include applicability, limitations and verification in SKILL.md, and learning_scope: global_candidate. "
        "Include a Python resource scripts/normalize.py that reads a JSON array of strings from stdin and prints the "
        "trimmed array as JSON. Declare validation_suite: normalize-headers-v1 and validation_entry: scripts/normalize.py. "
        "The independent suite will check Unicode separately. Retain one concise synthetic project memory. "
        "Use native memory and skill_manage tools. This probe is not a real Codex task or App conversation."
    )
    correction = (
        "Correction: synthetic header normalization needs Unicode NFKC before stripping whitespace; preserve letter case. "
        "Full-width letters, the ligature ﬁ and circled digits require compatibility normalization. "
        "Revise the existing learned-header-normalization skill and scripts/normalize.py, reading them with skill_view "
        "first. Do not create another skill. Add these counterexamples to verification, retain the stdin/stdout JSON "
        "array contract and validation_suite: normalize-headers-v1. Use native skill_manage operations."
    )
    completion = "Synthetic observed task ended; independent validation results are recorded separately."
    manifest = workspace/"sources.json"
    for index,text in enumerate((initial,correction,completion)):
        path = workspace/("fixture-"+str(index)+".txt")
        path.write_text(text)
        register_source(manifest,synthetic=path)
    settings = Settings(home,project,source,live.python,capture_scope="global",checked_publication=True,
                        data_policy={"enabled":True,"source_manifest":str(manifest),"workflow_projection":True},
                        proactive=True,quality_checks=True,idle_seconds=0,idle_interval=0)
    if args.enterprise:
        from dataclasses import replace
        from hermes_codex_learning.enterprise import make_boundary
        policy_path=home/"enterprise-boundary.json"
        atomic_json(policy_path,make_boundary(settings))
        settings=replace(settings,data_policy={"enabled":True,"mode":"trusted_enterprise","enterprise_boundary":str(policy_path)})
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from hermes_codex_learning.semantic_review import install_semantic_policy
    from hermes_codex_learning.review_input import install_review_policy
    from hermes_codex_learning.daemon import Bridge
    from hermes_codex_learning.capture import hook
    from hermes_codex_learning.knowledge import reconcile
    from hermes_codex_learning.quality import verify
    ctx = PluginContext(PluginManifest(name="codex-learning-bridge",source="user"),get_plugin_manager())
    api = ctx.learning(version=1)
    guard = install_semantic_policy(api,settings,ctx)
    install_review_policy(api)
    bridge = Bridge(settings,api)
    output = workspace/"result.json"
    receipt = {"state":"started","real_provider":True,"actual_codex":False,"scope":"explicit synthetic contract",
               "model":{key:original["model"].get(key) for key in ("provider","default","api_mode")},
               "reasoning_effort":original.get("agent",{}).get("reasoning_effort"),"phases":[],"requests":[]}
    receipt["enterprise_mode"]=args.enterprise
    def audit(**event):
        payload = json.dumps({key:event.get(key) for key in ("request_messages","system_prompt")},ensure_ascii=False)
        receipt["requests"].append({"foreign_canary_present":marker in payload,"payload_bytes":len(payload.encode())})
        atomic_json(output,receipt)
    registration = ctx.register_hook("pre_api_request",audit)
    atomic_json(output,receipt)
    versions = []
    try:
        bridge.store.set("knowledge-index-v1",reconcile({},api.read(kind="context"),bootstrap_project="/unrelated"))
        for phase,prompt in (("initial",initial),("correction",correction)):
            for event in [{"hook_event_name":"UserPromptSubmit","prompt":prompt},
                          {"hook_event_name":"Stop","last_assistant_message":completion}]:
                hook(dict(event,cwd=str(project),session_id="semantic-probe",turn_id=phase),settings)
            bridge.ingest()
            bridge.idle_reviews()
            bridge.reviews()
            bridge.publish()
            value = api.read(kind="skill",name="learned-header-normalization",issue_receipt=False)
            versions.append(value["version"])
            proof = verify(value)
            receipt["phases"].append({"phase":phase,"version":value["version"],"validation":proof,
                                      "status":api.read(kind="status",detail="summary")})
            atomic_json(output,receipt)
        view = bridge.rpc("context",{"cwd":str(project),"query":"header normalization"})
        receipt.update(state="completed",skill_revised=len(set(versions))==2,
                       final_script_verified=receipt["phases"][-1]["validation"]["state"]=="passed",
                       project_memory_written=bool(view["memories"]),
                       isolation_verified=bool(receipt["requests"]) and all(not row["foreign_canary_present"] for row in receipt["requests"]),
                       foreign_memory_preserved=marker in (home/"memories/MEMORY.md").read_text())
        receipt["passed"] = all(receipt[key] for key in ("skill_revised","final_script_verified","project_memory_written",
                                                        "isolation_verified","foreign_memory_preserved"))
        if args.enterprise:
            shared=bridge.rpc("context",{"cwd":str(workspace/"other-project"),"query":"header normalization"})
            receipt["enterprise_shared"]=any(x.get("name")=="learned-header-normalization" and x.get("audience")=="enterprise" for x in shared["skills"])
            receipt["other_project_memory_empty"]=not shared["memories"]
            receipt["passed"]=receipt["passed"] and receipt["enterprise_shared"] and receipt["other_project_memory_empty"]
    except Exception as exc:
        receipt.update(state="failed",passed=False,error=type(exc).__name__+": "+str(exc))
    finally:
        receipt["finished_at"] = time.time()
        atomic_json(output,redact(receipt))
        bridge.store.close()
        registration.dispose()
        guard.dispose()
        api.close()
    print(json.dumps({key:value for key,value in receipt.items() if key not in {"phases","requests"}},ensure_ascii=False))
    return 0 if receipt.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
