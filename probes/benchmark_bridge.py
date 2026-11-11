"""Synthetic 100k-event scaling check; no live data, providers or services."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.store import atomic_json, digest

parser = argparse.ArgumentParser()
parser.add_argument("--output",type=Path,required=True)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix="hermes-scale-") as directory:
    root = Path(directory)
    settings = Settings(root/"home",root,root,Path(sys.executable))
    bridge = Bridge(settings,SimpleNamespace())
    db = bridge.store.db
    try:
        started = time.perf_counter()
        def rows():
            for index in range(100000):
                payload = {"hook_event_name":"PostToolUse","session_id":"c"+str(index%1000),
                           "turn_id":str(index),"cwd":directory,"tool_name":"workflow_test",
                           "tool_response":{"outcome":"completed"}}
                yield str(index),str(index),json.dumps(payload),float(index),float(index)
        db.executemany("INSERT INTO events VALUES (?,?,?,?,?,NULL)",rows())
        def turns():
            for index in range(100000):
                value = {"source":{"host":"fixture","project":directory,"chat":"c"+str(index%1000),"turn":str(index)},
                         "status":"completed","user":"synthetic task","assistant":"synthetic result","tools":{}}
                yield str(index),json.dumps(value),str(index),str(index),float(index),float(index)
        db.executemany("INSERT INTO turns VALUES (?,?,?,?,?,?)",turns())
        db.executemany("INSERT INTO latest_turns VALUES (?,?,?)",
                       [("c"+str(index%1000),str(index),float(index)) for index in range(99000,100000)])
        db.commit()
        bridge.store.set("recovery-cursor",db.execute("SELECT MAX(rowid) FROM events").fetchone()[0])
        seeded = time.perf_counter()-started
        start = time.perf_counter()
        old = {}
        for row in db.execute("SELECT payload FROM turns ORDER BY activity"):
            value = json.loads(row[0])
            old[value["source"]["chat"]] = value
        baseline = time.perf_counter()-start
        samples = []
        for _ in range(10):
            start = time.perf_counter()
            current = bridge.latest_turns()
            bridge.recover_context()
            samples.append(time.perf_counter()-start)
        result = {"synthetic":True,"events":100000,"turns":100000,"chats":1000,
                  "latest_turns":len(current),"seed_seconds":seeded,"old_scan_seconds":baseline,
                  "incremental_median_seconds":statistics.median(samples),
                  "incremental_p95_seconds":sorted(samples)[-1],
                  "idle_event_rows":db.execute("SELECT COUNT(*) FROM events WHERE rowid>?",
                                                (bridge.store.get("recovery-cursor"),)).fetchone()[0]}
        result["passed"] = len(current)==1000 and result["idle_event_rows"]==0 and statistics.median(samples)<baseline
        atomic_json(args.output,result)
        print(json.dumps(result))
    finally:
        bridge.store.close()
raise SystemExit(0 if result["passed"] else 1)
