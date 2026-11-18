"""The exact-release guard must survive native Python activation/relaunch."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class GuardedNativeUpdateTests(unittest.TestCase):
    def run_guarded(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve()
            package = source / "hermes_cli"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (source / "hermes_bootstrap.py").write_text('''
import os, sys
from pathlib import Path
if not getattr(sys, "_hermes_relaunched", False):
    prefix = ("import sys, runpy; sys.path.insert(0, " + repr(str(Path(__file__).parent))
              + "); sys.argv = " + repr(sys.argv) + "; sys._hermes_relaunched = True; ")
    if sys.argv[0] == "-c":
        body = "exec(" + repr(sys.orig_argv[sys.orig_argv.index("-c") + 1]) + ")"
    else:
        body = "runpy.run_path(" + repr(str(Path(sys.argv[0]).absolute())) + ", run_name='__main__')"
    os.execv(sys.executable, [sys.executable, "-I", "-B", "-c", prefix + body])
''')
            (package / "source_releases.py").write_text('''
from dataclasses import dataclass
OFFICIAL_REPOSITORY = "NousResearch/hermes-agent"
@dataclass
class SourceTarget:
    requested_channel: str
    channel: str
    repository: str
    commit: str
    version: str
    ahead: bool = False
def resolve_source_target(channel, git_cmd=None, cwd=None, **kwargs):
    if MODE.startswith("rate-limit"):
        raise ValueError("GitHub API rate limit reached (HTTP 403)")
    commit = "b" * 40 if MODE == "commit-drift" else "a" * 40
    return SourceTarget(channel, channel, OFFICIAL_REPOSITORY, commit, "0.23.2")
MODE = ''' + repr(mode) + '\n')
            (package / "main.py").write_text('''
import hermes_bootstrap
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
def main():
    from hermes_cli.source_releases import resolve_source_target
    resolve_source_target("stable", ["git"], PROJECT_ROOT)
    return 0
''')
            (source / "hermes").write_text(
                "from hermes_cli.main import main\nraise SystemExit(main())\n")
            for name in ("update_cmd.py", "update_lock.py"):
                (package / name).write_text("# Native contract fixture\n")
            def git(*args):
                return subprocess.check_output(["git", "-C", str(source), *args],
                                               stderr=subprocess.DEVNULL, text=True).strip()
            git("init")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "native launch contract")
            before = git("rev-parse", "HEAD")
            program = '''
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_codex_learning import updates
from hermes_codex_learning.official_runtime import guarded_native_update
source, before, mode = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
recipe = {"base_commit": before, "native_updater": {
    "protocol": "resolve_source_target-v1",
    "files": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in ("hermes_cli/source_releases.py", "hermes_cli/update_cmd.py", "hermes_cli/update_lock.py")}}}
updates.fetch_release = lambda settings: {"tag": "v0.23.2"}
updates.ls_remote_tag_commit = lambda *args, **kwargs: "b" * 40 if mode == "rate-limit-drift" else "a" * 40
raise SystemExit(guarded_native_update(source, before, "v0.23.2", "a" * 40, recipe))
'''
            environment = {key: value for key, value in os.environ.items()
                           if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}}
            environment["HOME"] = str(source / "home")
            (source / "home").mkdir()
            (source / ".git/info/exclude").write_text("home/\n")
            return subprocess.run(
                [sys.executable, "-I", "-B", "-c", program,
                 str(Path(__file__).resolve().parents[1] / "src"), str(source), before, mode],
                cwd=source, env=environment, capture_output=True, text=True, timeout=30)

    def test_verified_target_survives_native_relaunch(self):
        result = self.run_guarded("verified")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_relaunch_cannot_drop_commit_guard(self):
        result = self.run_guarded("commit-drift")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different target than the tested official release", result.stderr)

    def test_rate_limit_fallback_survives_native_relaunch(self):
        result = self.run_guarded("rate-limit")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rate_limit_fallback_still_rejects_remote_commit_drift(self):
        result = self.run_guarded("rate-limit-drift")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different target than the tested official release", result.stderr)
