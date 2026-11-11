import tempfile
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_learning.config import Settings
from hermes_codex_learning.store import atomic_json
from hermes_codex_learning.updates import Updater, UpdateError, git


class RuntimeRecipeTests(unittest.TestCase):
    def test_same_tag_recipe_update_preserves_the_verified_old_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            updater = Updater(Settings(root / "home", root, root / "old", Path("/usr/bin/python3")))
            folder = updater.directory / "candidates/v0.23.0-fixture"
            source = folder / "source"
            source.mkdir(parents=True)
            def run(*args):
                subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
            run("init")
            (source / "agent.py").write_text("base\n")
            run("add", ".")
            run("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
            commit = git(source, "rev-parse", "HEAD")
            (source / "agent.py").write_text("verified old adapter\n")
            run("add", ".")
            atomic_json(folder / "verification.json", {"id": folder.name, "state": "ready", "source": str(source),
                        "tag": "v0.23.0", "commit": commit, "source_tree": git(source, "write-tree"), "recipe": "old",
                        "gates": ["native-contracts", "bridge-contracts", "runtime-smoke"]})
            updater.settings = replace(updater.settings, source=source)
            newer = {"v0.23.0": {"base_commit": commit, "recipe": "new"}}
            with patch.object(updater, "recipes", return_value=newer):
                self.assertEqual(updater.current(), "v0.23.0")
                result = updater.check(fetch=lambda _: {"tag": "v0.23.0"})
                self.assertEqual(result["state"], "available")
                self.assertEqual(result["target"], "v0.23.0")
                (source / "agent.py").write_text("unverified change\n")
                with self.assertRaisesRegex(UpdateError, "modified"):
                    updater.current()
