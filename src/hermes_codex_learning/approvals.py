"""Project-scoped review of native pending writes; native Hermes owns the writes."""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

from .store import digest, redact

STATES = ("pending", "duplicate", "stale", "already_applied", "unsupported")
ID_PATTERN = re.compile(r"[a-f0-9]{8}")


def change_fingerprint(changes):
    return digest([{key: change[key] for key in ("action", "before", "after")} for change in changes])


def inspect_record(record, entries, patcher=None):
    """Describe the pinned proposal, without applying it or replacing its payload."""
    payload = record.get("payload", {})
    operations = payload.get("operations") if payload.get("action") == "batch" else [payload]
    state, reason, changes = "pending", "等待人工确认修改前后的内容", []
    if (payload.get("target") != "memory" or not isinstance(operations, list)
            or not operations or len(operations) > 100):
        state, reason, operations = "unsupported", "此入口只审核项目记忆修改", []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("action") not in {"add", "patch", "replace", "remove"}:
            state, reason = "unsupported", "原生提案包含不支持的操作"
            break
        action = operation["action"]
        before = operation.get("matched_entry", "") if action != "add" else ""
        replacement = operation.get("content") or operation.get("new_text") or ""
        old = operation.get("old_text") or ""
        if not all(isinstance(value, str) for value in (before, replacement, old)):
            state, reason = "unsupported", "原生提案内容格式不正确"
            break
        if action != "add" and not before:
            state, reason = "unsupported", "旧提案没有绑定原条目，需要重新提出修改"
        after = (before.replace(old, replacement, 1) if action == "patch" and old
                 else replacement if action in {"add", "replace"} else "")
        if action == "patch" and (not old or old not in before):
            state, reason = "unsupported", "修订提案的原文与绑定条目不一致"
        elif action == "patch" and patcher is not None:
            patched, error, _ = patcher(before, old, replacement)
            if patched is None:
                state, reason = "unsupported", "原生修订校验未通过：" + error
            else:
                after = patched
        if action != "add" and before and before not in entries and state != "unsupported":
            state, reason = "stale", "原条目已经变化，此提案不能直接应用"
        changes.append({"action": action, "before": before, "after": after,
                        "diff": "".join(difflib.unified_diff(
                            (before + ("\n" if before and not before.endswith("\n") else "")).splitlines(keepends=True),
                            (after + ("\n" if after and not after.endswith("\n") else "")).splitlines(keepends=True),
                            fromfile="修改前", tofile="修改后", lineterm="\n"))})
    if state == "stale" and changes and all(change["after"] and change["after"] in entries for change in changes):
        state, reason = "already_applied", "提案中的新内容已经存在，可丢弃这份旧提案"
    return {"record": record, "state": state, "reason": reason, "changes": changes}


class NativeApprovals:
    """A short-lived native process avoids a background fork's temporary store proxy."""
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, action, **params):
        script = Path(__file__).resolve().parents[2] / "scripts/native_approvals.py"
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}}
        environment.update(self.settings.native_environment())
        result = subprocess.run(
            [str(self.settings.interpreter), "-I", "-B", str(script),
             "--source", str(self.settings.source), "--home", str(self.settings.home)],
            input=json.dumps({"action": action, **params}), capture_output=True, text=True,
            env=environment, timeout=30,
        )
        if result.returncode:
            try:
                error = json.loads(result.stdout).get("error", "Native approval adapter failed")
            except (ValueError, AttributeError):
                error = "Native approval adapter failed; inspect the native runtime"
            raise RuntimeError(error)
        return json.loads(result.stdout)


class PendingReviews:
    def __init__(self, settings, store, native=None):
        self.settings, self.store = settings, store
        self.native = native or NativeApprovals(settings)

    def project(self, cwd):
        project = self.settings.project_for(cwd)
        if project is None:
            raise ValueError("An authorized absolute project cwd is required")
        return project

    def identifiers(self, project):
        identifiers = set()
        rows = self.store.db.execute(
            "SELECT payload FROM notices WHERE json_extract(payload,'$.project')=? "
            "AND json_extract(payload,'$.kind')='review.finished' ORDER BY seq DESC", (project,))
        for row in rows:
            for fact in json.loads(row[0]).get("staged", []):
                if not isinstance(fact, dict) or fact.get("tool") != "memory":
                    continue
                identifier = fact.get("result", {}).get("pending_id")
                if isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier):
                    identifiers.add(identifier)
        return sorted(identifiers)

    def list(self, cwd, identifier=None):
        project = self.project(cwd)
        from .enterprise import boundary, scrub
        if self.settings.data_policy.get("mode") == "trusted_enterprise" and not boundary(self.settings.data_policy):
            raise PermissionError("Enterprise learning binding is not active; pending bodies are withheld")
        identifiers = self.identifiers(project)
        if identifier is not None and identifier not in identifiers:
            raise PermissionError("Pending proposal is unavailable in this project")
        items = self.native("inspect", ids=identifiers) if identifiers else []
        proposals, canonical = [], {}
        memory_index = self.store.get("knowledge-index-v1", {}).get("memory", {})
        memory_proofs = self.store.get("semantic-memory-attestations", {})
        for item in sorted(items, key=lambda row: (row["record"].get("created_at", 0), row["record"]["id"])):
            record = item["record"]
            if record["id"] not in identifiers:
                raise ValueError("Native adapter returned a proposal outside the requested project")
            safe = scrub(record) if self.settings.data_policy.get("mode") == "trusted_enterprise" else redact(record)
            if safe != record:
                item = {**item, "state": "unsupported", "reason": "提案含受限内容，需要原生人工审核", "changes": []}
                record = {**safe, "summary": "受限提案"}
            targets = [digest(["MEMORY.md", change["before"]]) for change in item["changes"] if change["before"]]
            if any((memory_index.get(key) or memory_proofs.get(key) or {}).get("project") not in {None, project}
                   for key in targets):
                item = {**item, "state": "unsupported", "reason": "修改目标不属于本项目，需要原生人工审核", "changes": []}
                record = {**record, "summary": "目标超出本项目的提案"}
            revision = digest(item["record"])
            duplicate_of = None
            if item["state"] == "pending":
                key = change_fingerprint(item["changes"])
                if key in canonical:
                    duplicate_of = canonical[key]
                    item = {**item, "state": "duplicate", "reason": "相同修改已有待审提案，可丢弃重复项"}
                else:
                    canonical[key] = record["id"]
            proposals.append({"id": record["id"], "project": project, "revision": revision,
                              "summary": record.get("summary", ""), "created_at": record.get("created_at"),
                              "state": item["state"], "reason": item["reason"], "operations": item["changes"],
                              "duplicate_of": duplicate_of, "can_approve": item["state"] == "pending",
                              "can_reject": True})
        counts = Counter(proposal["state"] for proposal in proposals)
        revision = digest([(p["id"], p["revision"], p["state"]) for p in proposals])
        return {"project": project, "proposals": [p for p in proposals if identifier is None or p["id"] == identifier],
                "counts": {state: counts[state] for state in STATES}, "review_revision": revision,
                "decisions": [item for item in self.store.get("approval-history-v1", []) if item["project"] == project][-20:],
                "automatic_approval": False}

    def decide(self, cwd, identifier, revision, decision):
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Review the proposal and supply its exact revision before deciding")
        from .updates import exclusive
        with exclusive(self.settings.state / "approval-decisions.lock"):
            return self._decide(cwd, identifier, revision, decision)

    def _decide(self, cwd, identifier, revision, decision):
        view = self.list(cwd, identifier)
        if not view["proposals"]:
            raise RuntimeError("Pending proposal no longer exists; refresh the review")
        proposal = view["proposals"][0]
        if proposal["revision"] != revision:
            raise RuntimeError("Pending proposal changed after review; refresh before deciding")
        if decision == "approve" and not proposal["can_approve"]:
            raise RuntimeError("Pending proposal cannot be applied: " + proposal["reason"])
        from .enterprise import boundary
        enterprise = boundary(self.settings.data_policy)
        if self.settings.data_policy.get("mode") == "trusted_enterprise" and not enterprise:
            raise PermissionError("Enterprise learning binding changed before the decision")
        result = self.native(decision, id=identifier, revision=revision)
        audit = {"id": uuid.uuid4().hex, "project": view["project"], "pending_id": identifier,
                 "decision": decision, "revision": revision, "state": result["state"],
                 "applied": result.get("applied", False), "at": time.time(),
                 "pending_removed": result.get("pending_removed", False), "reason": result.get("reason")}
        history = self.store.get("approval-history-v1", [])
        self.store.set("approval-history-v1", (history + [audit])[-2000:])
        if result.get("applied"):
            origins = self.store.get("approved-memory-origins", {})
            proofs = self.store.get("semantic-memory-attestations", {})
            inherited = [proofs.get(digest(["MEMORY.md", op["before"]]), {})
                         for op in proposal["operations"] if op["before"]]
            proof = next((proof for proof in inherited if proof), {})
            if enterprise:
                proof = {**proof, "project": view["project"], "classification": "approved",
                         "boundary_id": enterprise["id"], "policy_version": enterprise["policy_version"]}
            for entry in result.get("written_entries", []):
                key = digest(["MEMORY.md", entry])
                origins[key] = {"project": view["project"], "pending_id": identifier, "revision": revision}
                if proof:
                    proofs[key] = {**proof, "basis": "user_approved_native_memory_change"}
            self.store.set("approved-memory-origins", origins)
            self.store.set("semantic-memory-attestations", proofs)
        self.store.set("approval-last-result", audit)
        return audit

    def prune(self, cwd, revision=None, *, apply=False):
        from .updates import exclusive
        with exclusive(self.settings.state / "approval-decisions.lock"):
            view = self.list(cwd)
            candidates = [p for p in view["proposals"] if p["state"] in {"duplicate", "stale", "already_applied"}]
            if not apply:
                return {"project": view["project"], "candidates": candidates,
                        "review_revision": view["review_revision"], "applied": False}
            if revision != view["review_revision"]:
                raise RuntimeError("Pending review queue changed after preview; refresh before cleanup")
            removed, skipped = [], []
            for candidate in candidates[:20]:
                latest = self.list(cwd, candidate["id"])["proposals"]
                if not latest or latest[0]["revision"] != candidate["revision"] or latest[0]["state"] == "pending":
                    skipped.append(candidate["id"])
                    continue
                result = self._decide(cwd, candidate["id"], candidate["revision"], "reject")
                (removed if result["pending_removed"] else skipped).append(candidate["id"])
            return {"project": view["project"], "removed": removed, "skipped": skipped,
                    "remaining": max(0, len(candidates) - 20), "applied": True}


def add_parsers(commands):
    parser = commands.add_parser("approvals", help="Inspect and explicitly decide native pending memory proposals")
    actions = parser.add_subparsers(dest="approval_action", required=True)
    for action in ("list", "show", "approve", "reject", "history", "prune"):
        sub = actions.add_parser(action)
        sub.add_argument("--settings")
        sub.add_argument("--cwd", required=True)
        if action in {"show", "approve", "reject"}:
            sub.add_argument("--id", required=True)
        if action in {"approve", "reject"}:
            sub.add_argument("--revision", required=True, help="Exact revision from the reviewed proposal")
        if action == "prune":
            sub.add_argument("--revision", help="Exact review_revision from the cleanup preview")
            sub.add_argument("--apply", action="store_true")
    sub = actions.add_parser("self-test", help="Run actual native approval contracts in temporary learning homes")
    sub.add_argument("--settings")


def self_test(settings):
    root = Path(__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}}
    environment.update(PYTHONPATH=str(root / "src"), HERMES_LEARNING_NATIVE_SOURCE=str(settings.source),
                       HERMES_APPROVAL_SELFTEST="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [str(settings.interpreter), "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_native_approvals.py", "-v"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=120,
    )
    summary = result.stdout + result.stderr
    total = re.search(r"Ran (\d+) tests? in", summary)
    skipped = re.search(r"skipped=(\d+)", summary)
    executed = int(total[1]) - (int(skipped[1]) if skipped else 0) if total else 0
    passed = result.returncode == 0 and executed > 0
    return {"state": "passed" if passed else "failed", "tests": executed,
            "scope": "isolated native memory approval contracts; not Mac UI or real-provider learning",
            "model_calls": 0, "live_knowledge_changed": False,
            **({} if passed else {"error": "Native approval self-test failed", "details": summary[-3000:]})}


def execute(args):
    from .config import Settings
    from .store import Store
    settings = Settings.load(args.settings)
    if args.approval_action == "self-test":
        return self_test(settings)
    store = Store(settings.state)
    try:
        reviews = PendingReviews(settings, store)
        if args.approval_action in {"approve", "reject"}:
            return reviews.decide(args.cwd, args.id, args.revision, args.approval_action)
        if args.approval_action == "history":
            project = reviews.project(args.cwd)
            return {"project": project, "decisions": [item for item in store.get("approval-history-v1", [])
                                                       if item["project"] == project]}
        if args.approval_action == "prune":
            return reviews.prune(args.cwd, args.revision, apply=args.apply)
        return reviews.list(args.cwd, getattr(args, "id", None))
    finally:
        store.close()
