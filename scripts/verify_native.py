"""Canonical native and integrated bridge gates using an explicit test interpreter."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from hermes_codex_learning.store import atomic_json
from hermes_codex_learning.updates import catalog, verify_source

parser = argparse.ArgumentParser()
parser.add_argument("--source",type=Path,required=True)
parser.add_argument("--python",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
args = parser.parse_args()
source, interpreter = args.source.resolve(), args.python.absolute()
recipe = catalog()["v0.23.0"]
verify_source(source,recipe)
subprocess.run([str(interpreter),"-I","-B","-c","import pytest"],check=True,capture_output=True,timeout=10)
args.output.parent.mkdir(parents=True,exist_ok=True)
environment = {key:value for key,value in os.environ.items() if key in {"PATH","HOME","USER","LOGNAME","LANG"}}
environment.update(HERMES_PYTHON=str(interpreter),HERMES_HOME=str(args.output.parent/"fixture-home"),
                   PYTHONDONTWRITEBYTECODE="1")
with args.output.with_suffix(".native.log").open("w") as log:
    native = subprocess.run(["bash","scripts/run_tests.sh","-j","2",*recipe["native_tests"]],
                            cwd=source,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
environment.update(PYTHONPATH=str(ROOT/"src"),HERMES_LEARNING_NATIVE_SOURCE=str(source))
with args.output.with_suffix(".bridge.log").open("w") as log:
    bridge = subprocess.run([str(interpreter),"-B","-m","unittest","discover","-s","tests","-v"],
                            cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=900)
record = {"scope":"offline native and bridge contracts; not real-provider/App/UI",
          "native_exit":native.returncode,"bridge_exit":bridge.returncode,
          "native_files":len(recipe["native_tests"]),"recipe":recipe["recipe"],
          "passed":native.returncode == bridge.returncode == 0}
atomic_json(args.output,record)
print(json.dumps(record))
raise SystemExit(0 if record["passed"] else 1)
