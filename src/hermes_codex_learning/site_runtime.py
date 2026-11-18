"""Keep native App CLI actions on the committed managed site view."""
from __future__ import annotations

from pathlib import Path


def bind_site_cli(source: Path, binding: Path, launcher: Path) -> None:
    from hermes_cli import _launchers

    source = source.resolve()
    binding, launcher = binding.absolute(), launcher.absolute()
    original = _launchers.runtime_command
    identity = (str(source), str(binding), str(launcher))
    if getattr(original, "_hermes_site_cli", None) == identity:
        return

    def site_command(repo_root, args=(), *, module="hermes_cli.main", code=None,
                     python=None, home=None):
        command = original(repo_root, args, module=module, code=code, python=python, home=home)
        if (Path(repo_root).resolve() != source or module != "hermes_cli.main"
                or code is not None or python is not None or home is not None):
            return command
        # Retain PM's stable Python tool, but let the managed launcher select
        # and lease the current verified dependency generation at child start.
        return [command[0], "-I", "-B", str(launcher), str(binding), *args]

    site_command._hermes_site_cli = identity
    _launchers.runtime_command = site_command
