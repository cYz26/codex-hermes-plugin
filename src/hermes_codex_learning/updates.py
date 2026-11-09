"""Pinned Hermes candidates, isolated verification and durable update requests.

Only the supervisor activates candidates. A release number is discovery evidence,
never permission to run an unreviewed core/patch combination.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from .config import NETWORK_ENV_KEYS, Settings
from .store import Store, atomic_json, digest

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"
RELEASE_API = "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest"
MECHANISM_PATHS = ("agent/background_review", "agent/curator", "agent/learning_", "agent/turn_context",
                   "agent/agent_init", "agent/codex_runtime", "tools/memory", "tools/skill",
                   "hermes_cli/plugins", "hermes_cli/plugin_isolation", "hermes_state")


class UpdateError(RuntimeError):
    pass


def version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise UpdateError("Expected an official stable tag such as v0.23.0")
    return tuple(map(int, value[1:].split(".")))


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plugin_hash(root: Path = ROOT) -> str:
    paths = []
    for name in ("src", "scripts", "tests", "hermes-plugin", "hooks", "skills", "patches"):
        paths.extend(p for p in (root / name).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc")
    paths.extend(root / name for name in ("pyproject.toml", ".codex-plugin/plugin.json", ".mcp.json"))
    return digest({str(p.relative_to(root)): file_hash(p) for p in sorted(paths)})


@contextlib.contextmanager
def exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Another update operation owns this learning home") from exc
        yield


def run_command(argv, *, cwd: Path, env: dict, log: Path, timeout: float = 900):
    """Bound the whole process group, including native test/build children."""
    with log.open("ab") as output:
        process = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    if code:
        raise UpdateError(f"{Path(str(argv[0])).name} exited {code}; see {log}")


def git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *args], text=True, capture_output=True, timeout=30,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise UpdateError(f"Cannot inspect Hermes source ({' '.join(args[:2])})")
    return result.stdout.strip()


def catalog(root: Path = ROOT) -> dict:
    index = read_json(root / "patches/compatibility.json")
    if not index or index.get("schema") != 1 or index.get("learning_api") != 1:
        raise UpdateError("Unsupported compatibility catalog")
    result = {}
    for tag, relative in index["releases"].items():
        version(tag)
        path = (root / "patches" / relative).resolve()
        if not path.is_relative_to((root / "patches").resolve()):
            raise UpdateError("Compatibility manifest escapes patches directory")
        manifest = read_json(path)
        if (manifest["tag"] != tag or manifest["upstream"] != UPSTREAM
                or not re.fullmatch(r"[a-f0-9]{40}", manifest["base_commit"])):
            raise UpdateError("Invalid pinned upstream identity")
        patch = path.parent / manifest.get("patch_file", "native-learning-api-v1.patch")
        if not patch.resolve().is_relative_to((root / "patches").resolve()):
            raise UpdateError("Patch escapes patches directory")
        if file_hash(patch) != manifest["patch_sha256"]:
            raise UpdateError("Learning patch checksum mismatch")
        for name, checksum in manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise UpdateError("Invalid patched file identity")
        result[tag] = {**manifest, "patch_path": str(patch), "native_tests": index["native_tests"],
                       "manifest_path": str(path), "recipe": digest([manifest, index["native_tests"], index["learning_api"]])}
    return result


def verify_source(source: Path, recipe: dict) -> None:
    if not (source / ".git").exists():
        from .source import verify_source as verify_archive
        verify_archive(source, Path(recipe["manifest_path"]))
        return
    if git(source, "rev-parse", "HEAD") != recipe["base_commit"]:
        raise UpdateError("Hermes source is not the pinned official commit")
    for relative, checksum in recipe["files"].items():
        path = source / relative
        if not path.is_file() or path.is_symlink() or file_hash(path) != checksum:
            raise UpdateError(f"Hermes Learning API file differs: {relative}")


def source_seal(source: Path, recipe: dict) -> str:
    verify_source(source, recipe)
    # --index applies the patch to both index and worktree. Later unstaged edits
    # invalidate the candidate, including edits outside the Learning API files.
    if git(source, "diff", "--no-ext-diff", "--name-only"):
        raise UpdateError("Candidate source changed after patch application")
    if git(source, "ls-files", "--others", "--exclude-standard"):
        raise UpdateError("Candidate contains untracked source files")
    return git(source, "write-tree")


def github_json(settings: Settings, url: str) -> dict:
    proxies = {key.removesuffix("_proxy"): value for key, value in
               {**os.environ, **settings.network_environment}.items() if key in {"http_proxy", "https_proxy"}}
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        value = settings.network_environment.get(key) or os.environ.get(key)
        if value:
            proxies.setdefault(key.lower().removesuffix("_proxy"), value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "hermes-codex-learning"})
    with opener.open(request, timeout=20) as response:
        data = json.loads(response.read(8 * 1024 * 1024))
    return data


def fetch_release(settings: Settings) -> dict:
    try:
        data = github_json(settings, RELEASE_API)
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 429}:
            raise
        request = urllib.request.Request("https://github.com/NousResearch/hermes-agent/releases/latest",
                                         headers={"User-Agent": "hermes-codex-learning"})
        with urllib.request.urlopen(request, timeout=20) as response:
            target = response.geturl()
        prefix = "https://github.com/NousResearch/hermes-agent/releases/tag/"
        if not target.startswith(prefix):
            raise UpdateError("Official latest-release redirect did not select a stable tag") from exc
        tag = target[len(prefix):]
        version(tag)
        return {"tag": tag, "published_at": None, "release_id": None, "url": target,
                "source": "official_latest_stable_redirect", "api_rate_limited": True}
    tag = data["tag_name"]
    version(tag)
    if data.get("draft") is not False or data.get("prerelease") is not False:
        raise UpdateError("Latest release is not a published stable release")
    return {"tag": tag, "published_at": data.get("published_at"), "release_id": data["id"],
            "url": f"https://github.com/NousResearch/hermes-agent/releases/tag/{tag}"}


def official_commit(settings: Settings, tag: str) -> str:
    version(tag)
    base = "https://api.github.com/repos/NousResearch/hermes-agent/git/"
    try:
        obj = github_json(settings, base + "ref/tags/" + tag)["object"]
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 429}:
            raise
        result = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", UPSTREAM,
                                 "refs/tags/" + tag, "refs/tags/" + tag + "^{}"],
                                env={**os.environ, **settings.network_environment, "GIT_TERMINAL_PROMPT": "0"},
                                capture_output=True, text=True, timeout=60, check=True)
        refs = {line.split()[1]: line.split()[0] for line in result.stdout.splitlines() if len(line.split()) == 2}
        commit = refs.get("refs/tags/" + tag + "^{}") or refs.get("refs/tags/" + tag)
        if not commit or not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise UpdateError("Official Git ref did not resolve") from exc
        return commit
    for _ in range(4):
        if obj.get("type") == "commit" and re.fullmatch(r"[a-f0-9]{40}", obj.get("sha", "")):
            return obj["sha"]
        if obj.get("type") != "tag" or not re.fullmatch(r"[a-f0-9]{40}", obj.get("sha", "")):
            break
        obj = github_json(settings, base + "tags/" + obj["sha"])["object"]
    raise UpdateError("Official tag did not resolve to a commit")


def assess_changes(comparison: dict, *, current: str, target: str) -> dict:
    """Conservative triage, not an assertion that code diffs prove semantics."""
    files = comparison.get("files", [])
    critical = [{key: item.get(key) for key in ("filename", "status", "additions", "deletions")}
                for item in files if any(item.get("filename", "").startswith(prefix)
                                        or item.get("previous_filename", "").startswith(prefix)
                                        for prefix in MECHANISM_PATHS)]
    incomplete = "files" not in comparison or len(files) >= 300
    return {"state": "mechanism_review_required" if critical or incomplete
            else "adaptation_required", "from": current, "to": target,
            "changed_mechanisms": critical, "comparison_may_be_truncated": incomplete,
            "reason": "Unreviewed release; complete source research must establish whether a redesign is necessary",
            "next": ["Review trigger/review/write/Curator/API/data changes", "Present impact and proposed upgrade plan",
                     "For a redesign, obtain the user's agreement before modifying the workflow"],
            "comparison_url": f"https://github.com/NousResearch/hermes-agent/compare/{current}...{target}"}


def review_gate(recipe: dict) -> None:
    review = recipe.get("mechanism_review", {})
    # The original manifest is the established, unchanged baseline.
    if recipe["tag"] == "v0.22.7" and not review:
        return
    if review.get("classification") == "compatible" and review.get("reason"):
        return
    if (review.get("classification") == "adapted" and review.get("reason")
            and review.get("agreed_plan")):
        return
    raise UpdateError("Self-evolution changes need an agreed redesign/upgrade plan before preparation")


class Updater:
    def __init__(self, settings: Settings, *, root: Path = ROOT):
        self.settings, self.root = settings, root
        self.directory = settings.home.parent / "updates" / digest(str(settings.home))[:16]
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.status_path = self.directory / "status.json"
        self.lock = self.directory / "update.lock"

    def recipes(self) -> dict:
        from .adaptation import installed_recipes
        return {**catalog(self.root), **installed_recipes(self)}

    def status(self) -> dict:
        transaction = read_json(self.directory / "transaction.json", {})
        return {"policy": self.settings.update_mode, "interval_seconds": self.settings.update_interval,
                "active_source": str(self.settings.source), "active_python": str(self.settings.interpreter),
                "official_source": str(self.settings.official_source) if self.settings.official_source else None,
                "upgrade_official": self.settings.upgrade_official,
                "dependency_mode": "official_shared" if self.settings.borrows_official_python else "private",
                "check": read_json(self.status_path, {"state": "not_checked"}),
                "official_check": read_json(self.directory / "official-status.json"),
                "official_update": read_json(self.directory / "official-update.json"),
                "candidate": read_json(self.directory / "candidate.json"),
                "transaction": {key: transaction[key] for key in ("id", "action", "state", "started_at",
                                "committed_at", "restored_at", "recovered_at", "error") if key in transaction},
                "activation_error": read_json(self.directory / "activation-error.json"),
                "worker_error": read_json(self.directory / "worker-error.json"),
                "site_runtime": read_json(self.directory / "site-runtime.json"),
                "request": read_json(self.directory / "request.json"),
                "auto_adapt": self.settings.auto_adapt,
                "adaptation": read_json(self.directory / "adaptation/status.json")}

    def current(self) -> str:
        return self.runtime_info(self.settings.source)["tag"]

    def runtime_info(self, source: Path) -> dict:
        """An active same-tag patch keeps its own sealed verification identity."""
        source = source.resolve()
        parent = source.parent
        if source.name == "source" and parent.parent == self.directory / "candidates":
            record = read_json(parent / "verification.json", {})
            if (record.get("state") != "ready" or record.get("source") != str(source)
                    or record.get("id") != parent.name
                    or not {"native-contracts", "bridge-contracts", "runtime-smoke"} <= set(record.get("gates", []))):
                raise UpdateError("Runtime has no matching immutable verification record")
            version(record["tag"])
            if (git(source, "rev-parse", "HEAD") != record.get("commit")
                    or git(source, "diff", "--name-only")
                    or git(source, "ls-files", "--others", "--exclude-standard")
                    or git(source, "write-tree") != record.get("source_tree")):
                raise UpdateError("Verified runtime source was modified")
            return {"tag": record["tag"], "base_commit": record["commit"], "recipe": record["recipe"]}
        if (source / ".git").exists():
            commit = git(source, "rev-parse", "HEAD")
        else:
            commit = read_json(source.parent / "source-provenance.json", {}).get("base_commit")
        for tag, recipe in self.recipes().items():
            if recipe["base_commit"] == commit:
                verify_source(source, recipe)
                return {"tag": tag, "base_commit": commit, "recipe": recipe["recipe"]}
        raise UpdateError("Active Hermes runtime is not in the compatibility catalog")

    def check(self, *, fetch=fetch_release, compare=github_json, installed_only=False) -> dict:
        with exclusive(self.lock):
            path = self.directory / "official-status.json" if installed_only else self.status_path
            previous = read_json(path, {})
            result = {"checked_at": time.time(), "last_successful_check": previous.get("last_successful_check")}
            try:
                current = self.current()
                recipes = self.recipes()
                installed = official_commit_id = None
                if self.settings.official_source:
                    official_commit_id = git(self.settings.official_source, "rev-parse", "HEAD")
                    installed = next((tag for tag, item in recipes.items()
                                      if item["base_commit"] == official_commit_id), None)
                    if installed is None:
                        tags = git(self.settings.official_source, "tag", "--points-at", "HEAD").splitlines()
                        installed = max((tag for tag in tags if re.fullmatch(r"v\d+\.\d+\.\d+", tag)),
                                        key=version, default=None)
                    result["official"] = {"tag": installed, "commit": official_commit_id}
                if official_commit_id and (installed is None or (
                        installed in recipes and recipes[installed]["base_commit"] != official_commit_id)):
                    result.update(state="mechanism_review_required", current=current,
                                  reason="Official checkout needs a reviewed stable commit and learning recipe")
                    atomic_json(path, result)
                    return result
                release = {"tag": installed or current, "origin": "local_official_install"} if installed_only else fetch(self.settings)
                tag = installed if installed and version(installed) > version(current) else release["tag"]
                state = "current" if version(tag) <= version(current) else "available"
                assessment = None
                if state == "available":
                    if tag in recipes:
                        try:
                            review_gate(recipes[tag])
                        except UpdateError as exc:
                            state, assessment = "plan_required", {"state": "plan_required", "reason": str(exc)}
                        else:
                            if official_commit_id and official_commit_id != recipes[tag]["base_commit"]:
                                state = "waiting_for_official"
                                result["reason"] = "The official installation has not selected this release"
                    else:
                        try:
                            comparison = compare(self.settings, "https://api.github.com/repos/NousResearch/hermes-agent/compare/"
                                                 + recipes[current]["base_commit"] + "..." + tag + "?per_page=1")
                            assessment = assess_changes(comparison, current=current, target=tag)
                        except Exception as exc:
                            assessment = {"state": "mechanism_review_required", "reason": "Cannot assess upstream mechanism changes",
                                          "error": f"{type(exc).__name__}: {exc}"}
                        state = assessment["state"]
                if ((state == "current" and official_commit_id == recipes[current]["base_commit"])
                        or (state == "available" and official_commit_id == recipes[tag]["base_commit"])):
                    from .official_runtime import inspect_official

                    try:
                        binding = inspect_official(self.settings.official_source, official_commit_id)
                    except UpdateError as exc:
                        state = "official_pending"
                        result["reason"] = str(exc)
                    else:
                        result["official"].update(python=binding["python"], stamp=binding["stamp"])
                        if state == "current" and (str(self.settings.python) != binding["python"]
                                                   or not self.settings.borrows_official_python):
                            state, tag = "available", current
                result.update(state=state, current=current, latest=release,
                              last_successful_check=result["checked_at"])
                if state == "available":
                    result["target"] = tag
                if state == "current" and self.settings.source.parent.parent == self.directory / "candidates":
                    active = self.runtime_info(self.settings.source)
                    if active["recipe"] != recipes[tag]["recipe"]:
                        result.update(state="available", target=tag, reason="Reviewed same-version learning adapter changed")
                if assessment:
                    result.update(assessment=assessment, reason=assessment["reason"])
                if state == "waiting_for_official":
                    result["target"] = tag
            except Exception as exc:
                result.update(state="check_failed", error=f"{type(exc).__name__}: {exc}")
            atomic_json(path, result)
            return result

    def environment(self, home: Path) -> dict:
        # Do not pass provider credentials, Python startup hooks or plugin-home
        # bindings into builds and fixture tests. Each candidate has its own PM store.
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", *NETWORK_ENV_KEYS}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        tools = (self.settings.official_source.parent / "tools" if self.settings.official_source
                 else Path(self.settings.native_environment()["HERMES_RUNTIME_DIR"]))
        return {**env, **self.settings.network_environment, "HERMES_HOME": str(home),
                "HERMES_RUNTIME_DIR": str(tools), "GIT_TERMINAL_PROMPT": "0",
                "PYTHONDONTWRITEBYTECODE": "1"}

    def prepare(self, tag: str, *, official_source: Path | None = None,
                upgrade_official: bool | None = None, preflight=False, _recipe: dict | None = None) -> dict:
        version(tag)
        official_source = None if preflight else (official_source or self.settings.official_source)
        upgrade_official = self.settings.upgrade_official if upgrade_official is None else upgrade_official
        with exclusive(self.lock):
            recipes = self.recipes()
            if _recipe is not None:
                recipes = {**recipes, tag: _recipe}
            if tag not in recipes:
                raise UpdateError("Release needs a reviewed compatibility recipe before preparation")
            recipe = recipes[tag]
            review_gate(recipe)
            existing = read_json(self.directory / "candidate.json")
            if (existing and existing.get("tag") == tag and existing.get("preflight_only", False) == preflight
                    and (not official_source or existing.get("upgrade_official", False) == upgrade_official)
                    and (existing.get("official_runtime") or {}).get("source") ==
                    (str(official_source.resolve()) if official_source else None)):
                try:
                    self.validate_candidate(existing, _recipe=_recipe)
                    return existing
                except (UpdateError, OSError):
                    pass
            candidate_id = tag + "-" + uuid.uuid4().hex[:12]
            folder = self.directory / "candidates" / candidate_id
            folder.mkdir(parents=True, mode=0o700)
            source, runtime = folder / "source", folder / "runtime"
            env = self.environment(folder / "fixture-home")
            Path(env["HERMES_HOME"]).mkdir(mode=0o700)
            record = {"id": candidate_id, "tag": tag, "state": "preparing", "started_at": time.time(),
                      "source": str(source), "python": str(runtime / "bin/python"),
                      "commit": recipe["base_commit"], "recipe": recipe["recipe"],
                      "plugin": plugin_hash(self.root), "gates": [], "logs": str(folder)}
            if preflight:
                record["preflight_only"] = True
            atomic_json(self.directory / "candidate.json", record)
            try:
                def command(name, argv, cwd=folder, timeout=900):
                    run_command(argv, cwd=cwd, env=env, log=folder / (name + ".log"), timeout=timeout)
                    record["gates"].append(name)
                    atomic_json(self.directory / "candidate.json", record)

                binding = None
                if official_source:
                    from .official_runtime import inspect_official

                    binding = inspect_official(official_source, recipe["base_commit"])
                    record.update(official_runtime=binding, python=binding["python"], upgrade_official=upgrade_official)
                    fetch_from, ref = binding["source"], binding["commit"]
                else:
                    if official_commit(self.settings, tag) != recipe["base_commit"]:
                        raise UpdateError("Official tag moved: upstream commit differs from approved recipe")
                    cache = self.directory / "source-cache" / recipe["base_commit"]
                    fetch_from = str(cache) if cache.exists() else UPSTREAM
                    ref = recipe["base_commit"] if cache.exists() else "refs/tags/" + tag
                command("git-init", ["git", "init", source])
                command("fetch", ["git", "-c", "http.version=HTTP/1.1", "-C", source, "fetch", "--depth=1",
                                  fetch_from, ref], timeout=900)
                command("checkout", ["git", "-C", source, "checkout", "--detach", "FETCH_HEAD"])
                if git(source, "rev-parse", "HEAD") != recipe["base_commit"]:
                    raise UpdateError("Official tag moved: fetched commit differs from approved recipe")
                command("patch-check", ["git", "-C", source, "apply", "--check", recipe["patch_path"]])
                command("patch", ["git", "-C", source, "apply", "--index", recipe["patch_path"]])
                record["source_tree"] = source_seal(source, recipe)
                interpreter = None
                if binding and existing and existing.get("state") == "ready" and existing.get("recipe") == recipe["recipe"]:
                    try:
                        interpreter = self.private_interpreter(existing)
                    except (UpdateError, OSError):
                        pass
                if interpreter is None:
                    extras = [arg for extra in self.settings.runtime_extras for arg in ("--extra", extra)]
                    command("build", [self.settings.interpreter, "-B", "-m", "pm.build_env", "--source", source,
                                      "--out", runtime, "--group", "dev", "--group", "test", *extras], cwd=source, timeout=1200)
                    interpreter = runtime / "bin/python"
                if binding:
                    record["fallback_python"] = str(interpreter)
                env["HERMES_PYTHON"] = str(interpreter)
                if not recipe["native_tests"] or any(not (source / test).is_file() for test in recipe["native_tests"]):
                    raise UpdateError("Required native learning regression is missing")
                command("native-contracts", ["bash", "scripts/run_tests.sh", "-j", "2", *recipe["native_tests"]],
                        cwd=source, timeout=1200)
                if self.settings.site_manifest:
                    from .site_updates import prepare_site
                    prepare_site(self, recipe, record, interpreter, command)
                env["PYTHONPATH"] = str(self.root / "src")
                command("bridge-contracts", [interpreter, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=self.root)
                smoke = [record["python"], "-B", self.root / "scripts/check_update_runtime.py",
                         "--source", source, "--workspace", folder / "smoke"]
                if binding:
                    smoke.extend(["--fallback-python", interpreter])
                command("runtime-smoke", smoke, cwd=self.root)
                if source_seal(source, recipe) != record["source_tree"] or plugin_hash(self.root) != record["plugin"]:
                    raise UpdateError("Source or plugin changed during verification")
                if binding and inspect_official(official_source, recipe["base_commit"]) != binding:
                    raise UpdateError("Official runtime changed during verification; prepare again")
                record.update(state="ready", verified_at=time.time(),
                              python_config=file_hash(interpreter.parent.parent / "pyvenv.cfg"),
                              scope="native_and_bridge_contracts_and_isolated_worker; not_live_App_or_real_provider")
            except Exception as exc:
                record.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            atomic_json(self.directory / "candidate.json", record)
            atomic_json(folder / "verification.json", record)
            return record

    def private_interpreter(self, record: dict) -> Path:
        """Read a previously verified private test/fallback environment."""
        python = Path(record.get("fallback_python") or record["python"])
        environment = python.parent.parent
        if (environment.name != "runtime" or environment.is_symlink()
                or environment.parent.parent != self.directory / "candidates"
                or python != environment / "bin/python"):
            raise UpdateError("Private candidate interpreter is outside the managed runtime")
        if file_hash(environment / "pyvenv.cfg") != record.get("python_config"):
            raise UpdateError("Private candidate environment changed after verification")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise UpdateError("Private candidate interpreter is unavailable")
        return python

    def validate_candidate(self, record: dict, *, _recipe: dict | None = None) -> None:
        recipe = _recipe or self.recipes().get(record.get("tag"))
        if not recipe or record.get("state") != "ready" or record.get("recipe") != recipe["recipe"]:
            raise UpdateError("Candidate is not verified for this compatibility recipe")
        if record.get("commit", recipe["base_commit"]) != recipe["base_commit"]:
            raise UpdateError("Candidate commit differs from the reviewed recipe")
        review_gate(recipe)
        if record.get("plugin") != plugin_hash(self.root):
            raise UpdateError("Plugin changed since candidate verification; prepare again")
        folder = self.directory / "candidates" / record["id"]
        if Path(record["source"]) != folder / "source":
            raise UpdateError("Candidate paths are outside the managed candidate")
        binding = record.get("official_runtime")
        if binding:
            from .official_runtime import inspect_official

            if (not record.get("fallback_python") or record["python"] != binding["python"]
                    or inspect_official(Path(binding["source"]), recipe["base_commit"]) != binding):
                raise UpdateError("Official runtime changed after candidate verification")
        elif Path(record["python"]) != folder / "runtime/bin/python" or record.get("fallback_python"):
            raise UpdateError("Candidate interpreter is outside the managed candidate")
        required = {"native-contracts", "bridge-contracts", "runtime-smoke"}
        if self.settings.site_manifest:
            from .site_updates import validate_site
            required.add("site-contracts")
            validate_site(record)
        if not required <= set(record.get("gates", [])):
            raise UpdateError("Candidate verification gates are incomplete")
        if source_seal(Path(record["source"]), recipe) != record.get("source_tree"):
            raise UpdateError("Candidate source changed after verification")
        self.private_interpreter(record)

    def request(self, action: str, *, automatic=False) -> dict:
        if action not in {"apply", "rollback"}:
            raise UpdateError("Unknown update action")
        with exclusive(self.lock):
            if (self.directory / "request.json").exists():
                raise UpdateError("An update request is already pending")
            if action == "apply":
                candidate = read_json(self.directory / "candidate.json", {})
                self.validate_candidate(candidate)
                if candidate.get("preflight_only"):
                    raise UpdateError("Preflight is only for the official upgrade; prepare from the published installation first")
                target = candidate["id"]
            else:
                previous = read_json(self.directory / "previous.json")
                if not previous:
                    raise UpdateError("No previous runtime is recorded")
                target = digest(previous)
            request = {"id": uuid.uuid4().hex, "action": action, "target": target, "created_at": time.time(),
                       "automatic": automatic}
            atomic_json(self.directory / "request.json", request)
            return {**request, "state": "queued", "activation": "managed supervisor at the next review boundary"}

    def advance_official(self, candidate: dict, before: str, config: Path) -> dict:
        from .official_runtime import inspect_official, run_official_update

        with exclusive(self.lock):
            settings = Settings.load(config)
            if (settings.update_mode != "auto" or not settings.upgrade_official
                    or settings.official_source != self.settings.official_source):
                return {"state": "cancelled_by_policy"}
            self.validate_candidate(candidate)
            if not candidate.get("preflight_only"):
                raise UpdateError("Official update needs a dedicated verified preflight")
            source = settings.official_source
            if git(source, "rev-parse", "HEAD") != before:
                return {"state": "official_changed"}
            path = self.directory / "official-update.json"
            previous = read_json(path, {})
            if (previous.get("state") in {"updating", "failed"} and previous.get("commit") == candidate["commit"]
                    and previous.get("plugin") == candidate["plugin"] and previous.get("before") == before):
                return previous  # A failed App update must not repeat unattended.
            record = {"state": "updating", "tag": candidate["tag"], "commit": candidate["commit"],
                      "before": before, "plugin": candidate["plugin"], "started_at": time.time(),
                      "preflight": candidate["id"], "log": str(Path(candidate["logs"]) / "official-update.log")}
            atomic_json(path, record)
            try:
                binding = inspect_official(source, before)
                run_official_update(binding, candidate["tag"], candidate["commit"], root=self.root,
                                    env=self.environment(source.parent), log=Path(record["log"]),
                                    host_recipe=next((value for value in self.recipes().values() if value["base_commit"] == before), None))
                if git(source, "rev-parse", "HEAD") != candidate["commit"]:
                    raise UpdateError("Official installer did not publish the tested commit")
                record["state"] = "published"
            except Exception as exc:
                record.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            record["finished_at"] = time.time()
            atomic_json(path, record)
            return record

    def cycle(self, *, installed_only=False, config: Path | None = None) -> dict:
        result = self.check(installed_only=installed_only) if installed_only else self.check()
        self.notice(result)
        if self.settings.update_mode != "auto":
            return result
        if (self.settings.auto_adapt and not installed_only and
                result["state"] in {"adaptation_required", "mechanism_review_required"}
                and result.get("latest", {}).get("tag")):
            from .adaptation import Adapter
            adapted = Adapter(self).run(result["latest"]["tag"])
            self.notice(adapted)
            if adapted["state"] != "verified":
                return adapted
            return self.cycle(config=config)
        preflight = result["state"] == "waiting_for_official" and self.settings.upgrade_official and config is not None
        if result["state"] != "available" and not preflight:
            return result
        target = result.get("target", result["latest"]["tag"])
        previous = read_json(self.directory / "candidate.json", {})
        old_binding = previous.get("official_runtime") or {}
        selected = result.get("official", {})
        if (previous.get("state") in {"failed", "failed_activation"}
                and previous.get("tag") == target
                and previous.get("plugin") == plugin_hash(self.root)
                and previous.get("preflight_only", False) == preflight
                and old_binding.get("stamp") == selected.get("stamp")
                and old_binding.get("python") == selected.get("python")):
            return previous  # Retry only after an adapter change or explicit `prepare`.
        candidate = self.prepare(target, preflight=True) if preflight else self.prepare(target)
        self.notice(candidate)
        if preflight and candidate["state"] == "ready":
            official = self.advance_official(candidate, selected["commit"], config)
            self.notice(official)
            if official["state"] in {"published", "official_changed"}:
                return self.cycle(installed_only=True, config=config)
            return official
        if candidate["state"] == "ready" and not (self.directory / "request.json").exists():
            self.request("apply", automatic=True)
        return candidate

    def notice(self, result: dict):
        summaries = {
            "plan_required": "Hermes 新版自进化机制可能需要重设计；已暂停升级，请先商定修改方案。",
            "mechanism_review_required": "Hermes 新版学习机制尚待核对；已保留当前版本，需要先确认流程兼容性。",
            "adaptation_required": "发现 Hermes 新版；缺少已验证的适配方案，继续使用当前版本。",
            "waiting_for_official": "已发现兼容的 Hermes 新版，学习服务将在官方安装升级后验证并跟进。",
            "adaptation_failed": "Hermes 自动适配未通过验证；已保留当前运行版本，详细原因见更新状态。",
            "unknown": "Hermes 适配作业被中断；请先检查持久记录，未自动重跑。",
            "failed": "Hermes 候选版本未通过验证，当前学习运行时保持运行。",
            "check_failed": "Hermes 版本检查失败，将按周期重试，当前学习继续运行。",
            "committed": "Hermes 学习运行时已通过兼容性验证并完成切换。",
            "rolled_back": "Hermes 新版启动检查失败，已恢复旧运行时和切换前学习数据。",
        }
        state = result.get("state")
        if state not in summaries:
            return
        identity = digest([state, result.get("tag") or result.get("latest", {}).get("tag"), result.get("error"),
                           result.get("id") if state in {"committed", "rolled_back"} else None])
        detail = {key: result[key] for key in ("state", "tag", "latest", "reason", "assessment", "error") if key in result}
        store = Store(self.settings.state)
        try:
            store.notice({"id": "update:" + identity, "kind": "update.notice", "message": summaries[state],
                          "receipt": summaries[state] + "\n\n" + json.dumps(detail, ensure_ascii=False, indent=2)})
            from .notices import publish
            publish(self.settings, {"id": "update:" + identity, "kind": "update.notice", "message": summaries[state],
                                   "receipt": summaries[state] + "\n" + json.dumps(detail, ensure_ascii=False, indent=2)})
        finally:
            store.close()
