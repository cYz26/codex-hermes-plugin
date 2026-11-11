"""Independent content contracts and bounded, networkless script verification."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .store import digest

ROOT = Path(__file__).resolve().parents[2]
SUITES = ROOT / "fixtures/skill-validation"


def field(content, name):
    parts = content.lstrip("\ufeff").split("---",2)
    if len(parts) != 3 or parts[0].strip():
        return None
    values = [line.split(":",1)[1].strip() for line in parts[1].splitlines() if line.startswith(name+":")]
    if len(values) != 1:
        return None
    value = values[0]
    if len(value)>1 and value[0] in {"'",'"'} and value[-1] == value[0]:
        value = value[1:-1]
    return value if re.fullmatch(r"[a-zA-Z0-9_./-]+",value) else None


def manifest_matches(value):
    root = Path(value["resource_dir"]).resolve()
    if not isinstance(value.get("files"),dict) or "SKILL.md" not in value["files"]:
        return False
    for name, expected in value["files"].items():
        path = root / name
        if (path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file()
                or path.stat().st_size > 2*1024*1024
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            return False
    return True


def verifier_hash():
    return digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in [Path(__file__), ROOT/"scripts/isolated_skill.py", *sorted(SUITES.glob("*.json"))]})


def duplicate_key(value):
    content = re.sub(r"(?m)^(name|description):.*$", "", value.get("content", ""))
    return digest([" ".join(content.split()), {name:checksum for name,checksum in value["files"].items() if name != "SKILL.md"}])


def proof_valid(value, proof):
    return (proof.get("state") == "passed" and proof.get("version") == value["version"]
            and proof.get("manifest") == digest(value["files"]) and proof.get("verifier") == verifier_hash()
            and manifest_matches(value))


def verify(value):
    proof = {"version":value["version"], "manifest":digest(value["files"]), "verifier":verifier_hash(),
             "verified_at":time.time(), "state":"failed", "kind":"content_contract",
             "business_effectiveness":"not_inferred"}
    if not manifest_matches(value):
        return {**proof, "reason":"artifact_changed"}
    content = value.get("content") or ""
    if not all(re.search(pattern, content, re.I) for pattern in
               (r"applicab|适用", r"limit|constraint|限制|边界", r"verif|test|验证|测试")):
        return {**proof, "reason":"missing_applicability_limits_or_verification"}
    executable = [name for name in value["files"] if Path(name).suffix in {".py", ".sh"}]
    if not executable:
        return {**proof, "state":"passed", "cases":["applicability", "limits", "verification"],
                "scope":"content checks only; application still needs host evidence"}
    suite_name, entry = field(content, "validation_suite"), field(content, "validation_entry")
    if not suite_name or not re.fullmatch(r"[a-z0-9-]+", suite_name):
        return {**proof, "state":"pending", "reason":"independent_suite_required"}
    suite_path = SUITES / (suite_name + ".json")
    if not suite_path.is_file() or entry not in executable:
        return {**proof, "state":"pending", "reason":"unknown_suite_or_entry"}
    if not shutil.which("bwrap") or not Path("/usr/bin/python3").is_file():
        return {**proof, "state":"pending", "reason":"verified_sandbox_unavailable"}
    suite = json.loads(suite_path.read_text())
    cases = suite["cases"]
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="hermes-skill-verify-") as directory:
        temp = Path(directory)
        snapshot = temp/"artifact"
        snapshot.mkdir()
        for name in value["files"]:
            target = snapshot/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(value["resource_dir"])/name, target)
        if not manifest_matches({**value, "resource_dir":str(snapshot)}):
            return {**proof, "reason":"artifact_changed_during_copy"}
        command = ["bwrap","--unshare-all","--die-with-parent","--new-session","--clearenv",
                   "--ro-bind","/usr","/usr","--proc","/proc","--dev","/dev","--tmpfs","/tmp"]
        for name in ("/bin", "/lib", "/lib64"):
            path = Path(name)
            if path.is_symlink():
                command += ["--symlink",os.readlink(path),name]
            elif path.exists():
                command += ["--ro-bind",name,name]
        command += ["--ro-bind",str(snapshot),"/work","--ro-bind",str(ROOT/"scripts/isolated_skill.py"),"/runner.py",
                    "--setenv","PATH","/usr/bin:/bin","--setenv","HOME","/tmp","--chdir","/work",
                    "/usr/bin/python3","-I","-B","/runner.py"]
        command += ["/usr/bin/python3","-I","-B","-c",
                    "import sys,runpy; p=sys.argv[1]; sys.path.insert(0,'/work'); sys.argv=[p]; runpy.run_path(p,run_name='__main__')",
                    "/work/"+entry] if entry.endswith(".py") else ["/bin/sh","/work/"+entry]
        for index, case in enumerate(cases):
            with (temp/"stdout").open("w+b") as out, (temp/"stderr").open("w+b") as err:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
                try:
                    process.communicate(json.dumps(case["input"], ensure_ascii=False).encode(), timeout=60)
                    out.seek(0)
                    raw = out.read(1024*1024)
                    try:
                        matched = process.returncode == 0 and json.loads(raw) == case["expected"]
                    except ValueError:
                        matched = False
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    matched = False
                outcomes.append({"case":index, "passed":matched, "exit_code":process.returncode})
                if not matched:
                    break
    return {**proof, "state":"passed" if len(outcomes) == len(cases) and all(row["passed"] for row in outcomes) else "failed",
            "kind":"isolated_execution", "suite":suite_name, "cases":outcomes,
            "scope":"fixed synthetic contract on Linux; no general correctness claim"}
