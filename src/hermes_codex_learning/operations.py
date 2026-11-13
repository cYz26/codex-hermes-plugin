"""Explicit, previewable operations for installation, policy and verification."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import Settings
from .store import Store, atomic_json, digest


def add_parsers(commands):
    curator = commands.add_parser('curator-config', help='Preview or enable native Curator consolidation')
    curator.add_argument('--settings')
    curator.add_argument('--apply', action='store_true')
    retry = commands.add_parser("review-retry", help="Preview, retry or close a discovered review attempt")
    retry.add_argument("--settings")
    retry.add_argument("--attempt", required=True)
    retry.add_argument("--apply", action="store_true")
    retry.add_argument("--close-without-retry", action="store_true")
    retry.add_argument("--note", help="Reason for closing a blocked review without retry")
    retry.add_argument("--evidence", help="Audit evidence for the closure")
    enterprise=commands.add_parser("enterprise-config",help="Bind operator-authorized enterprise learning to this host and model configuration")
    enterprise.add_argument("--settings")
    enterprise.add_argument("--apply",action="store_true")
    enterprise.add_argument("--disable",action="store_true",help="Restore the pre-enterprise data policy")
    install = commands.add_parser("install", help="Preview or install an immutable learning bundle")
    install.add_argument("--settings")
    install.add_argument("--start", action="store_true")
    group = install.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    sources = commands.add_parser("sources", help="Register explicitly selected public or synthetic evidence")
    sources.add_argument("--manifest",type=Path,required=True)
    group = sources.add_mutually_exclusive_group(required=True)
    group.add_argument("--public-url")
    group.add_argument("--synthetic",type=Path)
    sources.add_argument("--apply",action="store_true")
    learning = commands.add_parser("learning-config", help="Preview or enable the agreed proactive learning policy")
    learning.add_argument("--settings")
    learning.add_argument("--source-manifest",type=Path)
    learning.add_argument("--apply",action="store_true")
    retention = commands.add_parser("retention", help="Inspect retention or queue a bounded cleanup")
    retention.add_argument("--settings")
    retention.add_argument("--apply",action="store_true")
    for name, description in (("memory-preview", "Inspect project memory and preview compaction"),
                              ("publication-preview", "Inspect which skill versions pass strict visibility"),
                              ("review-metrics", "Summarize one day of recorded review costs and changes"),
                              ("skill-readiness", "Check an exact skill version on this execution host")):
        preview = commands.add_parser(name, help=description)
        preview.add_argument("--settings")
        preview.add_argument("--cwd", required=True)
        if name == "memory-preview":
            preview.add_argument("--proposals", type=Path, help="JSON with revision and replacements by entry ID")
        elif name == "review-metrics":
            preview.add_argument("--date", required=True)
            preview.add_argument("--timezone", default="UTC")
            preview.add_argument("--acceptance-chat", action="append", default=[])
            preview.add_argument("--audit-chat", action="append", default=[])
        elif name == "skill-readiness":
            preview.add_argument("--name", required=True)
            preview.add_argument("--artifact-version", required=True)
    quality = commands.add_parser("quality", help="Verify an immutable native skill artifact")
    quality.add_argument("--settings")
    quality.add_argument("--name",required=True)
    migration = commands.add_parser("migration", help="Prepare, seal and verify a separate site migration")
    migration.add_argument("action",choices=["prepare","seal","verify","status"])
    migration.add_argument("--out",type=Path,required=True)
    migration.add_argument("--source",type=Path)
    migration.add_argument("--official-source",type=Path)
    migration.add_argument("--python",type=Path)
    migration.add_argument("--tests",nargs="+")
    migration.add_argument("--apply",action="store_true")


def execute(args):
    if args.command in {"memory-preview", "publication-preview"}:
        from .ipc import request
        params = {"cwd": args.cwd}
        if args.command == "memory-preview" and args.proposals:
            params["proposals"] = json.loads(args.proposals.read_text())
        return request(Settings.load(args.settings), args.command.replace("-", "_"), params)
    if args.command == "skill-readiness":
        from .ipc import request
        from .skill_reads import check_requirements
        value = request(Settings.load(args.settings), "skill_requirements",
                        {"cwd": args.cwd, "name": args.name, "artifact_version": args.artifact_version})
        return {"name": value["name"], "version": value["version"],
                **check_requirements(value["requirements"], scope="execution_host")}
    if args.command == "review-metrics":
        from zoneinfo import ZoneInfoNotFoundError
        from .review_metrics import daily
        from .store import ReadStore
        settings = Settings.load(args.settings)
        project = settings.project_for(args.cwd)
        if project is None:
            raise ValueError("Review metrics require an in-scope absolute working directory")
        store = ReadStore(settings.state)
        try:
            return daily(store, project, args.date, args.timezone,
                         acceptance_chats=args.acceptance_chat, audit_chats=args.audit_chat)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use a valid IANA time zone") from exc
        finally:
            store.close()
    if args.command == 'curator-config':
        from .ipc import request
        return request(Settings.load(args.settings), 'configure_curator', {'dry_run': not args.apply}, timeout=5)
    if args.command == "review-retry":
        from .ipc import request
        params = {"attempt_id": args.attempt, "dry_run": not args.apply}
        if args.close_without_retry:
            params.update(close_without_retry=True, note=args.note, evidence=args.evidence)
        elif args.note is not None or args.evidence is not None:
            raise ValueError("Closure notes require --close-without-retry")
        return request(Settings.load(args.settings), "review_retry", params, timeout=5)
    if args.command=="enterprise-config":
        from .enterprise import make_boundary
        from .updates import exclusive, Updater
        path=Settings.configuration_path(args.settings);settings=Settings.load(path)
        receipt=settings.state/"enterprise-boundary.json"
        backup=settings.state/"before-enterprise-policy.json"
        if args.disable:
            if not backup.is_file():
                raise ValueError("No pre-enterprise policy is recorded")
            policy=json.loads(backup.read_text())["data_policy"]
            proposed={"mode":"restored_previous_policy"}
        else:
            record=make_boundary(settings)
            policy={**settings.data_policy,"enabled":True,"mode":"trusted_enterprise",
                    "workflow_projection":True,"enterprise_boundary":str(receipt)}
            proposed={"mode":"trusted_enterprise","ordinary_enterprise_body":True,
                      "sharing_audience":"enterprise","credentials":"filtered",
                      "explicit_restrictions":"preserved","model_config_sha256":record["model_config_sha256"]}
        if args.apply:
            with exclusive(Updater(settings).lock):
                current=json.loads(path.read_text())
                if not args.disable:
                    if not backup.exists():
                        atomic_json(backup,{"data_policy":current.get("data_policy",{})})
                    atomic_json(receipt,record)
                current["data_policy"]=policy
                atomic_json(path,current)
        return {"settings":str(path),"policy":proposed,"applied":args.apply,"restart_required":args.apply}
    if args.command == "install":
        from .installation import install
        return install(args)
    if args.command == "sources":
        from .assessment import register_source, public_url
        if args.public_url:
            public_url(args.public_url)
        if not args.apply:
            return {"dry_run":True,"manifest":str(args.manifest),"source":args.public_url or str(args.synthetic)}
        return register_source(args.manifest.absolute(),url=args.public_url,synthetic=args.synthetic)
    if args.command == "learning-config":
        from .updates import exclusive, Updater
        path = Settings.configuration_path(args.settings)
        settings = Settings.load(path)
        changes = {"learning":{"proactive":True,"quality_checks":True},"retention":{"enabled":True}}
        if args.source_manifest:
            from .assessment import load_sources
            load_sources(args.source_manifest.absolute())
            changes["data_policy"] = {**settings.data_policy,"enabled":True,"workflow_projection":True,
                                      "source_manifest":str(args.source_manifest.absolute())}
        if args.apply:
            from .ipc import request
            capabilities = request(settings,"capabilities",timeout=3).get("native",{})
            if not all(capabilities.get(name) is True for name in ("artifact_preview","bounded_status","retention")):
                raise RuntimeError("Activate the verified 0.5 native patch before enabling the new policy")
            with exclusive(Updater(settings).lock):
                current = json.loads(path.read_text())
                atomic_json(path.with_name("bridge.before-learning-v05.json"), current)
                for key, value in changes.items():
                    current[key] = {**current.get(key,{}),**value}
                atomic_json(path,current)
        return {"settings":str(path),"changes":changes,"applied":args.apply,"restart_required":args.apply}
    if args.command in {"retention","quality"}:
        from .ipc import request
        settings = Settings.load(args.settings)
        if args.command == "retention":
            return request(settings,"retention",{"dry_run":not args.apply},timeout=30)
        from .quality import verify
        value = request(settings,"artifact_read",{"name":args.name})
        proof = verify(value)
        store = Store(settings.state)
        try:
            store.set("quality:"+digest([args.name,value["version"]]),proof)
            store.set("publication-context-version",None)
        finally:
            store.close()
        return proof
    if args.command == "migration":
        from . import migration
        if args.action == "status":
            return json.loads((args.out/"migration.json").read_text())
        if args.action == "prepare":
            if not args.source or not args.official_source:
                raise ValueError("Migration preparation needs --source and --official-source")
            if not args.apply:
                return {"dry_run":True,"inventory":migration.source_inventory(args.source),"out":str(args.out)}
            return migration.prepare(args.source,args.official_source,args.out)
        if not args.apply:
            return {"dry_run":True,"operation":args.action,"out":str(args.out),"tests":args.tests}
        if args.action == "seal":
            record = migration.seal(args.out)
            source = Path(record["candidate"])
            tests = args.tests or [name for name in migration.changed_files(source) if name.startswith("tests/") and name.endswith(".py")]
            from .updates import file_hash
            if any(not (source/name).resolve().is_relative_to(source.resolve()) or not (source/name).is_file() for name in tests):
                raise ValueError("Regression paths must name files in the candidate")
            record["tests"] = {name:file_hash(source/name) for name in tests}
            atomic_json(args.out/"migration.json",record)
            return record
        record = json.loads((args.out/"migration.json").read_text())
        if not args.python or not record.get("tests"):
            raise ValueError("Verification needs --python with pytest and a sealed test set")
        subprocess.run([str(args.python),"-I","-B","-c","import pytest"],check=True,capture_output=True,timeout=10)
        environment = {key:value for key,value in os.environ.items() if key in {"PATH","HOME","USER","LOGNAME","LANG"}}
        environment.update(HERMES_PYTHON=str(args.python),HERMES_HOME=str(args.out.absolute()/"fixture-home"))
        with (args.out/"verification.log").open("w") as log:
            result = subprocess.run(["bash","scripts/run_tests.sh","-j","2",*record["tests"]],
                                    cwd=record["candidate"],env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
        if result.returncode:
            raise RuntimeError("Site verification failed; see verification.log")
        record["state"] = "verified"
        atomic_json(args.out/"migration.json",record)
        return record
    raise ValueError("Unknown operation")
