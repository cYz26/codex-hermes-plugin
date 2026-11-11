#!/usr/bin/env python3
"""Launch a verified site view; managed updates always return to the verifier."""
import json
import os
import sys
from pathlib import Path

binding_path = Path(sys.argv[1])
binding = json.loads(binding_path.read_text())
if binding.get("state") != "ready":
    raise SystemExit("Hermes site binding is not ready")
args = sys.argv[2:]
python = Path(binding["python"])
if not python.is_file():
    python = Path(binding.get("fallback_python") or "")
if not python.is_file():
    raise SystemExit("Verified Hermes interpreter is unavailable; inspect bridge update status")
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "src"))
if args and args[0] == "update":
    if any(value not in {"--yes", "--channel", "stable"} for value in args[1:]):
        raise SystemExit("Managed Hermes supports verified stable updates; use bridge update for policy and status")
    from hermes_codex_learning.cli import main
    sys.argv = ["bridge", "update", "run", "--settings", binding["learning_settings"]]
    raise SystemExit(main())
# Keep the native in-process bootstrap shape visible to Hermes' PID/entrypoint
# discovery. A private script name alone is not proof of gateway ownership.
program = (
    "import sys\nfrom pathlib import Path\n"
    + "sys.path.insert(0, " + repr(str(root / "src")) + ")\n"
    + "sys.path.insert(0, " + repr(binding["source"]) + ")\n"
)
if str(python) == binding["python"] and binding.get("fallback_python"):
    program += ("from hermes_codex_learning.official_runtime import lease_official_python\n"
                + "lease_official_python(Path(" + repr(str(python)) + "))\n")
program += "from hermes_cli.main import main\nsys.argv[0] = 'hermes'\nmain()\n"
os.execv(str(python), [str(python), "-I", "-B", "-c", program, *args])
