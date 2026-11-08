"""Run one Hermes worker; the stdlib supervisor owns interpreter selection."""
import os
import signal
import sys
import threading
from pathlib import Path

settings = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.config import Settings

configuration = Settings.load(settings)
os.environ.update(configuration.native_environment())
if sys.platform == "darwin" and sys.version_info[:2] == (3, 14):
    sys.modules["_ruamel_yaml"] = None

if os.environ.get("HERMES_LEARNING_SUPERVISED") == "1":
    def parent_lifetime():
        os.read(sys.stdin.fileno(), 1)  # Raw fd: no buffered lock held during Python shutdown.
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=parent_lifetime, daemon=True).start()

sys.path.insert(0, str(configuration.source))
if configuration.fallback_python is not None and Path(sys.executable) == configuration.python:
    from hermes_codex_learning.official_runtime import lease_official_python

    lease_official_python(configuration.python)
sys.argv = ["hermes", "learning-bridge", "--settings", str(settings)]
from hermes_cli.main import main

main()
