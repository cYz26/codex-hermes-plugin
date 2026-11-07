"""Start the bridge through Hermes's registered plugin CLI, not private agent imports."""
import os
import sys
from pathlib import Path

settings = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.config import Settings

configuration = Settings.load(settings)
os.environ.update(configuration.native_environment())
# The optional C YAML parser stalled the worker's threads on macOS CPython 3.14.
# Let ruamel select its Python parser before importing Hermes in this process.
if sys.platform == "darwin" and sys.version_info[:2] == (3, 14):
    sys.modules["_ruamel_yaml"] = None
sys.path.insert(0, str(configuration.source))
sys.argv = ["hermes", "learning-bridge", "--settings", str(settings)]
from hermes_cli.main import main

main()
