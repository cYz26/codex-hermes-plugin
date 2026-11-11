"""Resume only the captured supervisor if its replacing installer disappears."""
import argparse
import os
import signal
import time
from pathlib import Path


def start_time(pid):
    try:
        path = Path("/proc")/str(pid)
        if path.stat().st_uid != os.getuid():
            return None
        return (path/"stat").read_text().rsplit(")",1)[1].split()[19]
    except (OSError,IndexError):
        return None


def watch(pid, expected, owner, owner_expected, deadline):
    while start_time(pid) == expected:
        if start_time(owner) != owner_expected or time.monotonic() >= deadline:
            try:
                os.kill(pid,signal.SIGCONT)
            except ProcessLookupError:
                pass
            return
        time.sleep(0.2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pid",type=int)
    parser.add_argument("expected")
    parser.add_argument("owner",type=int)
    parser.add_argument("owner_expected")
    parser.add_argument("deadline",type=float)
    args = parser.parse_args()
    watch(args.pid,args.expected,args.owner,args.owner_expected,args.deadline)
