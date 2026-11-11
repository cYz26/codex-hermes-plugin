import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class ServiceGuardTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux service guard")
    def test_stopped_supervisor_is_resumed_if_installer_disappears(self):
        script = Path(__file__).resolve().parents[1]/"scripts/service_guard.py"
        spec = importlib.util.spec_from_file_location("service_guard",script)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory)/"resumed"
            program = "import os,signal; from pathlib import Path; os.kill(os.getpid(),signal.SIGSTOP); Path("+repr(str(marker))+").write_text('resumed')"
            target = subprocess.Popen([sys.executable,"-c",program])
            try:
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    raw = Path("/proc")/str(target.pid)/"stat"
                    if raw.exists() and raw.read_text().rsplit(")",1)[1].strip().startswith("T"):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Synthetic supervisor did not stop")
                guard.watch(target.pid,guard.start_time(target.pid),999999999,"absent",time.monotonic()+2)
                self.assertEqual(target.wait(timeout=5),0)
                self.assertEqual(marker.read_text(),"resumed")
            finally:
                if target.poll() is None:
                    target.kill()
                    target.wait()
