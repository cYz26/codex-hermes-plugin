import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.site_runtime import bind_site_cli


class SiteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "old-source"
        self.binding = self.root / "active-binding.json"
        self.launcher = Path(__file__).resolve().parents[1] / "scripts/site-launcher.py"
        self.calls = []
        def native(repo_root, args=(), **kwargs):
            self.calls.append((repo_root, list(args), kwargs))
            return [sys.executable, "-I", "-c", "raise RuntimeError('no dependency environment is committed')", *args]
        self.native = native
        self.launchers = types.ModuleType("hermes_cli._launchers")
        self.launchers.runtime_command = native
        package = types.ModuleType("hermes_cli")
        package._launchers = self.launchers
        self.modules = patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli._launchers": self.launchers})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.temporary.cleanup()

    def test_app_gateway_action_uses_verified_launcher_and_preserves_profile(self):
        bind_site_cli(self.source, self.binding, self.launcher)
        args = ["-p", "coder", "gateway", "restart", "--interrupt"]
        cmd = self.launchers.runtime_command(self.source, args)
        self.assertEqual(cmd, [sys.executable, "-I", "-B", str(self.launcher), str(self.binding), *args])
        self.assertEqual(self.calls[-1][1], args)

    def test_foreign_install_and_explicit_native_commands_keep_their_owner(self):
        bind_site_cli(self.source, self.binding, self.launcher)
        for source, kwargs in [(self.root / "other-source", {}), (self.source, {"module": "pm"}),
                               (self.source, {"code": "print(1)"}), (self.source, {"python": "/external/python"}),
                               (self.source, {"home": "/other/home"})]:
            with self.subTest(source=source, kwargs=kwargs):
                cmd = self.launchers.runtime_command(source, ["--help"], **kwargs)
                self.assertEqual(cmd[1:3], ["-I", "-c"])
                self.assertNotIn(str(self.launcher), cmd)

    def test_binding_is_installed_once(self):
        bind_site_cli(self.source, self.binding, self.launcher)
        first = self.launchers.runtime_command
        bind_site_cli(self.source, self.binding, self.launcher)
        self.assertIs(self.launchers.runtime_command, first)

    def test_old_app_action_follows_current_binding_at_child_start(self):
        home = self.root / "home"
        home.mkdir()
        newer = self.root / "new-source"
        for source, version in [(self.source, "old"), (newer, "new")]:
            package = source / "hermes_cli"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (source / "hermes_bootstrap.py").write_text("")
            (package / "_launchers.py").write_text(
                "import sys\ndef runtime_command(root,args=(),**kwargs):\n"
                "    return [sys.executable,'-I','-c','raise RuntimeError(\\\"no dependency environment is committed\\\")',*args]\n")
            (package / "main.py").write_text(
                "import json,sys\ndef main():\n    print(json.dumps({'version':" + repr(version)
                + ", 'args':sys.argv[1:]}))\n")
        self.binding.write_text(json.dumps({"state": "ready", "source": str(self.source), "python": sys.executable}))
        bind_site_cli(self.source, self.binding, self.launcher)
        args = ["gateway", "restart", "--help"]
        command = self.launchers.runtime_command(self.source, args)
        self.binding.write_text(json.dumps({"state": "ready", "source": str(newer), "python": sys.executable}))
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL"}}
        env.update(HOME=str(home), HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(command, cwd=self.root, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"version": "new", "args": args})
