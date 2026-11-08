"""Stable stdlib supervisor; Hermes itself runs in a replaceable child."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hermes_codex_learning.supervisor import main

raise SystemExit(main(sys.argv[1]))
