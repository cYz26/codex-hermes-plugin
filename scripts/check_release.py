#!/usr/bin/env python3
"""Offline checks for the publishable project; never runs a model or installs a plugin."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".venv", "backups", "build", "dist"}


def published_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_NAMES for part in relative.parts):
            continue
        if relative.parts[:2] == ("evidence", "private") or relative.name == "UPGRADE-CHECKPOINT.json":
            continue
        if path.is_file():
            yield path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_versions_and_patch():
    plugin = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    module = ast.parse((ROOT / "src/hermes_codex_learning/__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    assert plugin["version"] == package["project"]["version"] == version
    native_plugin = (ROOT / "hermes-plugin/plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*" + re.escape(version) + r"\s*$", native_plugin, re.M)
    manifest = json.loads((ROOT / "patches/manifest.json").read_text(encoding="utf-8"))
    patch = ROOT / "patches/native-learning-api-v1.patch"
    assert digest(patch) == manifest["patch_sha256"], "patch bytes differ from manifest"
    paths = re.findall(r"^diff --git a/(.+) b/\1$", patch.read_text(encoding="utf-8"), re.M)
    assert set(paths) == set(manifest["files"]) and len(paths) == len(set(paths))
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in paths)
    assert re.fullmatch(r"[a-f0-9]{40}", manifest["base_commit"])
    source = ast.parse((ROOT / "src/hermes_codex_learning/cli.py").read_text(encoding="utf-8"))
    base = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "BASE" for target in node.targets))
    assert base == manifest["base_commit"], "installer and patch target different Hermes bases"
    acceptance = json.loads((ROOT / "ACCEPTANCE.json").read_text(encoding="utf-8"))
    assert acceptance["upstream"]["base_commit"] == base
    assert acceptance["upstream"]["patch_sha256"] == manifest["patch_sha256"]
    # App/provider evidence keeps the version actually exercised; an updater
    # release must not relabel historical learning acceptance as its own.
    assert tuple(map(int, acceptance["implementation_version"].split("."))) <= tuple(map(int, version.split(".")))
    sys.path.insert(0, str(ROOT / "src"))
    from hermes_codex_learning.updates import catalog
    catalog(ROOT)
    assert digest(ROOT / "HERMES-CODEX-PROBE.json") == acceptance["historical_probe_sha256"]
    assert isinstance(acceptance["verification"]["actual_app_hook_acceptance"], bool)
    if acceptance["verification"]["actual_app_hook_acceptance"]:
        closed_loop = acceptance["verification"]["app_closed_loop_evidence"]
        assert (ROOT / closed_loop).is_file(), "real App acceptance needs its own evidence record"
        check_app_closed_loop(json.loads((ROOT / closed_loop).read_text(encoding="utf-8")))
    if acceptance.get("global_rollout", {}).get("deployed"):
        check_global_rollout(json.loads((ROOT / acceptance["global_rollout"]["evidence"]).read_text()))
    cross_project = acceptance.get("cross_project_verification", {})
    if cross_project.get("two_project_automatic_learning"):
        check_cross_project_learning(cross_project)
    if acceptance.get("finalization", {}).get("state") == "passed":
        check_finalization(acceptance, acceptance["implementation_version"], base,
                           current_sources=acceptance["implementation_version"] == version)
    assert acceptance["real_provider_probe"]["input_provenance"] == "synthetic_acceptance_fixture_not_app_hooks"
    assert (ROOT / "scripts/bridge").stat().st_mode & 0o111, "hook launcher is not executable"
    for record in json.loads((ROOT / "evidence/INDEX.json").read_text(encoding="utf-8"))["records"]:
        if record["visibility"] == "repository":
            assert digest(ROOT / record["path"]) == record["sha256"], record["path"]


def check_finalization(acceptance, version, base, *, current_sources=True):
    """Bind the release claims to the exercised package, worker and host receipts."""
    final = acceptance["finalization"]
    assert final["candidate_version"] == version
    data = {key: json.loads((ROOT / path).read_text()) for key, path in final["evidence"].items()}
    assert all(record["status"] == "passed" and record["version"] == version for record in data.values())
    curator = data["curator"]
    worker = digest(ROOT / "scripts/serve.py") if current_sources else curator["worker_entry_sha256"]
    assert re.fullmatch(r"[a-f0-9]{64}", worker)
    assert curator["worker_entry_sha256"] == worker and curator["trigger"] == "scheduled_worker_poll"
    assert curator["not_due_skipped"] and curator["active_scope_skipped"] and curator["same_interval_not_repeated"]
    assert curator["run_count"] == 1 and curator["native_model_calls"] == 0
    assert curator["transitions"]["marked_stale"] == curator["transitions"]["archived"] == 1
    assert curator["lock_preserved"] and curator["archive_bytes_preserved"]
    notice = data["notification"]
    assert notice["dispatch_accepted"] and notice["user_confirmed_click"] and notice["model_calls"] == 0
    assert notice["marker"] == notice["cua_marker_observed"] and notice["cua_window_title"] == "Hermes 学习明细"
    if current_sources:
        assert notice["notifier_source_sha256"] == digest(ROOT / "macos/HermesLearningNotifier.swift")
    budget = data["budget"]
    assert len(budget["cases"]) >= 3 and all(0 < row["additional_context_utf8_bytes"] <= 256 for row in budget["cases"])
    assert budget["worker_launch_entry"]["sha256"] == worker
    if current_sources:
        assert all(row["sha256"] == digest(ROOT / row["path"]) for row in budget["runtime_files"])
    else:
        assert all(re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) for row in budget["runtime_files"])
    recovery = data["recovery"]
    assert recovery["worker_entry_sha256"] == worker and recovery["ipc_healthy"]
    assert 0 < recovery["previous_pending_checkpoint"] == recovery["previous_pending_retained"] == recovery["previous_pending_delivered"]
    assert all(state in {"interrupted", "unknown"} for state in recovery["previous_review_states"])
    recall = data["recall"]
    assert recall["origin"] == "Codex_App_create_thread_with_live_hooks"
    learning = acceptance["cross_project_verification"]
    training = json.loads((ROOT / learning["training_evidence"]).read_text())["projects"]
    assert set(recall["projects"]) == set(training)
    assert {row["project"] for row in budget["host_hook_trust"] if row["trusted"] == 7
            and not row["trust_changed_by_agent"] and row["source_package"] == version} == {
                row["project"] for row in training.values()}
    for lane, row in recall["projects"].items():
        assert row["projection"] == "projected" and not row["foreign_label_in_transcript"]
        assert row["project"] == training[lane]["project"] and row["label_sha256"] == training[lane]["expected_label_sha256"]
        assert row["hook_context_bytes"] and all(0 < size <= 256 for size in row["hook_context_bytes"])
    for name, count, trigger in (("threshold_cost", 10, "native_threshold"), ("idle_cost", 1, "idle_flush")):
        cost = data[name]
        assert cost["native_commit"] == base and cost["workload_cases"] == count and cost["worker_entry_sha256"] == worker
        assert cost["actual_fee_usd"] is None and cost["bridge"]["review"] == trigger
        assert cost["bridge"]["auxiliary"]["background_review"]["api_calls"] > 0
        assert cost["native"]["review"] == (trigger if count == 10 else "none_below_threshold")
        assert all(cost[side]["checks"] == count and cost[side]["model"] == acceptance["learning_model"]["model"]
                   for side in ("bridge", "native"))


def check_global_rollout(record):
    """Global enablement is separate from a new multi-project end-to-end learning cycle."""
    assert record["status"] == "global_enabled_and_current_App_capture_verified"
    assert record["native_patch_changed"] is False and record["native_model_auth_and_write_policy_config_unchanged"]
    assert not record["trust_database_modified_by_agent"]
    evidence = {key: json.loads((ROOT / path).read_text()) for key, path in record["evidence"].items()}
    runtime, trust = evidence["runtime"], evidence["host_trust"]
    assert runtime["version"] == record["version"] and runtime["capture_scope"] == "global"
    assert runtime["runtime_files"] and all(item["matches"] for item in runtime["runtime_files"])
    assert runtime["foreign_project_memory_withheld"] and runtime["missing_cwd_memory_withheld"]
    assert len(trust["records"]) >= 2
    assert all(item["ready"] and item["trust_changed"] is False for item in trust["records"])
    app, mcp = evidence["actual_App_capture"], evidence["actual_App_MCP"]
    assert app["input_provenance"] == "actual_Codex_App_hooks_after_user_retrust"
    assert app["counts"]["UserPromptSubmit"] > 0 and app["counts"]["PostToolUse"] > 0
    events = {event["id"]: event for event in app["events"]}
    assert app["native_skill_deliveries"]
    for receipt in app["native_skill_deliveries"]:
        event = events[receipt["source_event_id"]]
        assert event["delivered"] and event["tool"] == "mcp__hermes_learning__skill_load"
        assert receipt["status"] == "recorded" and receipt["evidence"]["stage"] == "body_delivered"
        assert receipt["evidence"]["source_event"] == [event["chat"], event["turn"], event["tool_call"]]
    assert mcp["actual_App_MCP"] and mcp["shared_skill_found"]
    assert mcp["own_project"] != mcp["other_project"]
    assert mcp["own_memory_targets"] and not mcp["other_project_memory_targets"]
    assert record["new_multi_project_App_learning_cycle"] is False, "A broader claim needs new independent App evidence"


def check_cross_project_learning(record):
    """Validate the native writes, scope readback and shared skill delivery."""
    scope = json.loads((ROOT / record["scope_probe_evidence"]).read_text())
    assert all(probe["ready"] for probe in scope["official_hooks_list"])
    assert scope["scope_preserved"] and not scope["foreign_scope_context_delivered"]
    live_probe = scope["after_both_project_memories_published"]
    assert not live_probe["foreign_scope_context_delivered"]
    assert live_probe["probe_events"] and all(event["delivered"] and event["cwd"] != event["bridge_project"]
                                              for event in live_probe["probe_events"])
    training = json.loads((ROOT / record["training_evidence"]).read_text())["projects"]
    host = json.loads((ROOT / record["training_host_evidence"]).read_text())["projects"]
    scoped = json.loads((ROOT / record["scoped_context_evidence"]).read_text())
    assert not any(scoped["before"]["labels_already_in_published_memories"].values())
    assert not any(scoped["after"]["unscoped"]["labels_present"].values())
    assert training["A"]["project"] != training["B"]["project"]
    assert training["A"]["chat"] != training["B"]["chat"]
    assert training["A"]["expected_label_sha256"] != training["B"]["expected_label_sha256"]
    for lane, learned in training.items():
        project = learned["project"]
        assert learned["prompt_contains_neither_label"] and learned["events_all_delivered"]
        assert learned["events"]["UserPromptSubmit"] and learned["terminal_kind"] == "Stop"
        assert learned["bound_projects"] == [project]
        assert any(observation["id"] == learned["terminal_event"] and observation["projection"] == "projected"
                   for observation in learned["observations"])
        host_turn = host[lane]
        assert host_turn["turn"] == learned["turn"] and host_turn["thread"]["id"] == learned["chat"]
        assert host_turn["status"] == "completed" and host_turn["thread"]["cwd"] == project
        exit_codes = {item["id"]: item["exit_code"] for item in host_turn["command_executions"]}
        assert any(exit_codes.get(call["call_id"]) == 0 and call["checks"] and all(call["checks"].values())
                   and call["own_label_in_result"] and not call["other_label_in_result"] for call in learned["fixture_calls"])
        assert any(attempt["status"] == "completed" and attempt["reason"] in {"native_threshold", "idle_flush"}
                   and attempt["memory_operations"] > 0 and attempt["uncertain"] == 0
                   for attempt in learned["attempts"])
        assert learned["published_memory"] and all(entry["project"] == project and not entry["other_label_in_entry"]
                                                   for entry in learned["published_memory"])
        view = scoped["after"][lane]
        assert view["project"] == project and view["own_in_full_scope"] and not view["other_in_full_scope"]
    shared = json.loads((ROOT / record["shared_skill_evidence"]).read_text())["during_two_project_loop"]
    assert shared["source_project"] == training["B"]["project"] and shared["target_project"] == training["A"]["project"]
    assert any(attempt["id"] == shared["source_attempt"] and shared["name"] in attempt["skills"]
               for attempt in training["B"]["attempts"])
    assert shared["deliveries"] and all(item["status"] == "recorded" and item["stage"] == "body_delivered"
                                        and item["caller_project"] == shared["target_project"]
                                        for item in shared["deliveries"])
    hashes = {item["path"]: item["content_hash"] for item in shared["artifact_files"]}
    assert all(item["version"] == shared["version"] and hashes[item["file_path"]] == item["content_hash"]
               for item in shared["deliveries"])
    assert not any(any(item["contains_test_labels"].values()) for item in shared["artifact_files"])
    if record.get("live_two_project_loop"):
        recall = json.loads((ROOT / record["recall_evidence"]).read_text())
        assert recall["status"] == "passed" and recall["version"] == record["version"]
        assert recall["origin"] == "Codex_App_create_thread_with_live_hooks"
        assert set(recall["projects"]) == set(training)
        observed = list(recall["projects"].values())
        assert len({row["chat"] for row in (*observed, *training.values())}) == 4
        assert len({row["source_event"] for row in observed}) == 2
        assert len({row["parent_chat"] for row in observed}) == 1
        for lane, row in recall["projects"].items():
            learned = training[lane]
            assert row["project"] == learned["project"] and row["training_chat"] == learned["chat"]
            assert row["label_sha256"] == learned["expected_label_sha256"]
            assert row["projection"] == "projected" and row["foreign_label_in_transcript"] is False
            assert row["hook_context_sha256"] and row["parent_tool_call"] and row["stop_event"]
            assert row["recall_path"] in {"hook", "scoped_mcp"}
            if row["recall_path"] == "scoped_mcp":
                assert any(tool["tool"] == "mcp__hermes_learning__context" and tool.get("own_label_returned")
                           for tool in row["tools"])


def check_app_closed_loop(record):
    """A success flag alone cannot stand in for App, review and delivery evidence."""
    assert record["status"] == "passed"
    a = json.loads((ROOT / record["evidence"]["A"]).read_text(encoding="utf-8"))
    b = json.loads((ROOT / record["evidence"]["B_captured"]).read_text(encoding="utf-8"))
    initial = json.loads((ROOT / record["evidence"]["B_initial"]).read_text(encoding="utf-8"))
    trust = json.loads((ROOT / record["evidence"]["host_trust"]).read_text(encoding="utf-8"))
    assert trust["ready"] and trust["trust_changed"] is False
    assert a["chat"] != b["chat"] == initial["chat"]
    assert a["hook_counts"]["PostToolUse"] > 0 and a["hook_counts"]["Stop"] > 0
    assert a["automatic_review"]["run_status"] == "completed"
    assert a["automatic_review"]["reason"] in {"native_threshold", "idle_flush"}
    assert a["automatic_review"]["operations"] and a["automatic_review"]["uncertain"] == 0
    assert initial["status"] == "completed"
    assert any(call["tool"] == "skill_load" and call["status"] == "completed" for call in initial["tool_calls"])
    assert b["business_tests"]["exit_code"] == 0 and b["business_tests"]["passed"] > 0
    assert b["initial_task_capture_gap_preserved"] is True and b["all_events_delivered"]
    assert b["hook_counts"]["PostToolUse"] > 0 and b["hook_counts"]["Stop"] > 0
    assert b["turns"] and all(t["observed"] and t["projection"] == "projected" for t in b["turns"])
    assert all(t["task_input_provenance"]["source_event"] for t in b["turns"])
    assert b["stage_claims"]["causal_quality_improvement_verified"] is False
    assert {r["file_path"] for r in b["skill_reads"]} >= {"SKILL.md", "references/receipt-evidence.md"}
    for read in b["skill_reads"]:
        artifact = a["learned_skill_snapshot_before_B_changes"][read["name"]]
        assert read["version"] == artifact["version"]
        assert read["content_hash"] == artifact["files"][read["file_path"]]
        delivery = read["native_delivery"]
        assert delivery["status"] == "recorded" and delivery["version"] == read["version"]
        assert delivery["evidence"]["stage"] == "body_delivered"
        assert delivery["evidence"]["source_event"] == read["source_event"]
        assert read["source_event"][0] == b["chat"]
        assert read["source_event"][1] in {t["source"]["turn"] for t in b["turns"]}
    followup = None
    if "B_automatic_review" in record["evidence"]:
        followup = json.loads((ROOT / record["evidence"]["B_automatic_review"]).read_text(encoding="utf-8"))
        assert followup["chat"] == b["chat"] and followup["run_status"] == "completed"
        assert followup["manual_review_trigger"] is False and followup["native_thresholds_changed"] is False
        assert followup["intent"]["reason"] in {"native_threshold", "idle_flush"}
        assert followup["uncertain"] == 0
    if record["gates"].get("user_system_notification_observed"):
        assert followup is not None
        assert followup["notification"]["result"]["dispatched"] is True
        assert followup["notification"]["user_visibility"] == "observed_by_user"


def check_links_and_credentials():
    errors = []
    patterns = {
        "private key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
        "API credential": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
        "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})\b"),
        "bearer credential": re.compile(r"(?i)Bearer\s+[A-Za-z0-9_./+-]{28,}"),
        "JWT credential": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    }
    for path in published_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule, pattern in patterns.items():
            if pattern.search(text):
                errors.append(f"{path.relative_to(ROOT)}: possible {rule}; value withheld")
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", text):
                target = unquote(target.strip("<>").split("#", 1)[0])
                if not target or "://" in target or target.startswith(("/", "#", "mailto:")):
                    continue
                if not (path.parent / target).exists():
                    errors.append(f"{path.relative_to(ROOT)}: missing local link {target}")
        if path.suffix == ".json":
            obj = json.loads(text)
            def find_live_receipts(value):
                if isinstance(value, dict):
                    if value.get("delivery_token"):
                        errors.append(f"{path.relative_to(ROOT)}: live delivery receipt in publishable JSON")
                    for item in value.values():
                        find_live_receipts(item)
                elif isinstance(value, list):
                    for item in value:
                        find_live_receipts(item)
            find_live_receipts(obj)
    if errors:
        raise RuntimeError("\n".join(errors))


def main():
    check_versions_and_patch()
    check_links_and_credentials()
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    if result.returncode:
        return result.returncode
    print("Release checks passed: package, patch, evidence, links, credential scan, and bridge tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
