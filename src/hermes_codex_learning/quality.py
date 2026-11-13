"""Independent content contracts and bounded, networkless script verification."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from .store import digest

ROOT = Path(__file__).resolve().parents[2]
SUITES = ROOT / "fixtures/skill-validation"
RUNNER = ROOT / "scripts/isolated_skill.py"
MAC_SANDBOX = "/usr/bin/sandbox-exec"
MAC_RESIDENT_LIMIT = 512*1024**2
MAC_POLICY = """(version 1)
(deny default)
(allow sysctl-read)
(allow file-read*
    (subpath (param "runtime")) (subpath (param "work"))
    (subpath "/System/Library") (subpath "/usr/lib") (subpath "/usr/share/locale")
    (literal (param "runner") "/" "/bin/bash" "/dev/null" "/dev/random" "/dev/urandom"))
(allow file-map-executable
    (subpath (param "runtime")) (subpath "/System/Library") (subpath "/usr/lib"))
(allow process-exec (literal (param "python") "/bin/bash"))
"""


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
    files = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [Path(__file__), RUNNER, *sorted(SUITES.glob("*.json"))]}
    backend = sandbox_backend()
    runtime_identity = None
    if backend == "sandbox-exec":
        runtime, python = _mac_python()
        runtime_identity = [str(runtime), hashlib.sha256(python.read_bytes()).hexdigest()]
    return digest([files, sys.platform, os.uname().release if hasattr(os, "uname") else None, backend, runtime_identity])


def duplicate_key(value):
    content = re.sub(r"(?m)^(name|description):.*$", "", value.get("content", ""))
    return digest([" ".join(content.split()), {name:checksum for name,checksum in value["files"].items() if name != "SKILL.md"}])


def proof_valid(value, proof):
    return (proof.get("state") == "passed" and proof.get("version") == value["version"]
            and proof.get("manifest") == digest(value["files"]) and proof.get("verifier") == verifier_hash()
            and manifest_matches(value))


def sandbox_backend() -> str | None:
    if sys.platform == "linux" and Path("/usr/bin/python3").is_file() and shutil.which("bwrap"):
        return "bwrap"
    if sys.platform == "darwin" and os.access(MAC_SANDBOX, os.X_OK) and _mac_python():
        return "sandbox-exec"
    return None


def _mac_python():
    for developer in ("/Library/Developer/CommandLineTools", "/Applications/Xcode.app/Contents/Developer"):
        runtime = (Path(developer) / "Library/Frameworks/Python3.framework/Versions/Current").resolve()
        interpreter = runtime / "Resources/Python.app/Contents/MacOS/Python"
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            continue
        if all(path.stat().st_uid == 0 and not path.stat().st_mode & 0o022 for path in (runtime, interpreter)):
            return runtime, interpreter
    return None


def _invocation(backend, snapshot):
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    if backend == "sandbox-exec":
        runtime, interpreter = _mac_python()
        runner = snapshot.parent / "runner.py"
        shutil.copyfile(RUNNER, runner)
        command = [MAC_SANDBOX, "-D", "runtime="+str(runtime), "-D", "work="+str(snapshot),
                   "-D", "python="+str(interpreter), "-D", "runner="+str(runner), "-p", MAC_POLICY]
        env.update(HOME=str(snapshot), TMPDIR=str(snapshot))
        return command, str(interpreter), str(runner), str(snapshot), env
    command = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for name in ("/bin", "/lib", "/lib64"):
        path = Path(name)
        if path.is_symlink():
            command += ["--symlink", os.readlink(path), name]
        elif path.exists():
            command += ["--ro-bind", name, name]
    command += ["--ro-bind", str(snapshot), "/work", "--ro-bind", str(RUNNER), "/runner.py",
                "--setenv", "PATH", env["PATH"], "--setenv", "LC_ALL", env["LC_ALL"],
                "--setenv", "HOME", "/tmp", "--chdir", "/work"]
    return command, "/usr/bin/python3", "/runner.py", "/work", env


def _wait_case(process, payload, *, monitor_resident):
    deadline = time.monotonic() + 60
    failure = "timeout"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            process.communicate(payload, timeout=min(remaining, 0.15) if monitor_resident else remaining)
            return None
        except subprocess.TimeoutExpired:
            payload = None
        if monitor_resident and process.poll() is None:
            try:
                sample = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                                        capture_output=True, timeout=1, env={"LC_ALL": "C"})
                if int(sample.stdout) * 1024 > MAC_RESIDENT_LIMIT:
                    failure = "resident_memory_limit"
                    break
            except (OSError, ValueError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    failure = "resident_memory_sampling_unavailable"
                    break
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    return failure


def verify(value):
    proof = {"version":value["version"], "manifest":digest(value["files"]), "verifier":verifier_hash(),
             "verified_at":time.time(), "state":"failed", "kind":"content_contract",
             "business_effectiveness":"not_inferred"}
    if not manifest_matches(value):
        return {**proof, "reason":"artifact_changed"}
    content = value.get("content") or ""
    patterns = {
        "applicability": r"applicab|适用|何时使用|使用场景|\bwhen\s+to\s+(?:use|apply)\b|\buse\s+(?:this\s+skill\s+)?when\b",
        "limits": r"limit|constraint|限制|边界|\b(?:outside (?:its|the) scope|out of scope|not supported)\b|不适用",
        "verification": r"verif|test|验证|测试",
    }
    checks = {name: bool(re.search(pattern, content, re.I)) for name, pattern in patterns.items()}
    proof["checks"] = checks
    if not all(checks.values()):
        return {**proof, "reason":"missing_applicability_limits_or_verification",
                "missing_sections": [name for name, present in checks.items() if not present]}
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
    backend = sandbox_backend()
    if backend is None:
        return {**proof, "state":"pending", "reason":"verified_sandbox_unavailable"}
    suite = json.loads(suite_path.read_text())
    cases = suite["cases"]
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="hermes-skill-verify-",
                                     dir="/tmp" if backend == "sandbox-exec" else None) as directory:
        temp = Path(directory).resolve()
        snapshot = temp/"artifact"
        snapshot.mkdir()
        for name in value["files"]:
            target = snapshot/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(value["resource_dir"])/name, target)
        if not manifest_matches({**value, "resource_dir":str(snapshot)}):
            return {**proof, "reason":"artifact_changed_during_copy"}
        command, interpreter, runner, work, env = _invocation(backend, snapshot)
        python_flags = ["-I", "-B", "-S"] if backend == "sandbox-exec" else ["-I", "-B"]
        command += [interpreter, *python_flags, runner]
        if backend == "sandbox-exec":
            marker = temp / "outside"
            marker.write_text("synthetic")
            try:
                probe = subprocess.run([*command, "--probe", str(marker)], cwd=snapshot, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                return {**proof, "state": "pending", "backend": backend, "reason": "verified_sandbox_unavailable"}
            if probe.returncode:
                return {**proof, "state": "pending", "backend": backend, "reason": "verified_sandbox_unavailable"}
        if entry.endswith(".py"):
            command += [interpreter, *python_flags, "-c",
                        "import os,runpy,sys; p=sys.argv[1]; sys.path.insert(0,os.getcwd()); sys.argv=[p]; runpy.run_path(p,run_name='__main__')",
                        work+"/"+entry]
        else:
            shell = ["/bin/bash", "--noprofile", "--norc", "--posix"] if backend == "sandbox-exec" else ["/bin/sh"]
            command += [*shell, work+"/"+entry]
        for index, case in enumerate(cases):
            with (temp/"stdout").open("w+b") as out, (temp/"stderr").open("w+b") as err:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                           cwd=snapshot, env=env, start_new_session=True)
                failure = _wait_case(process, json.dumps(case["input"], ensure_ascii=False).encode(),
                                     monitor_resident=backend == "sandbox-exec")
                out.seek(0)
                try:
                    matched = failure is None and process.returncode == 0 and json.loads(out.read(1024*1024)) == case["expected"]
                except ValueError:
                    matched = False
                outcomes.append({"case":index, "passed":matched, "exit_code":process.returncode})
                if failure:
                    outcomes[-1]["reason"] = failure
                if not matched:
                    break
    return {**proof, "state":"passed" if len(outcomes) == len(cases) and all(row["passed"] for row in outcomes) else "failed",
            "kind":"isolated_execution", "backend":backend, "suite":suite_name, "cases":outcomes,
            "address_space_limit_bytes": 1024**3 if backend == "bwrap" else None,
            "sampled_resident_limit_bytes": MAC_RESIDENT_LIMIT if backend == "sandbox-exec" else None,
            "scope":"fixed synthetic contract"}
