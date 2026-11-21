"""Run the native auxiliary reviewer without access to the bridge's write connection."""
import json
import sys
from pathlib import Path

sys.path[:0] = [sys.argv[1], str(Path(__file__).resolve().parents[1] / "src")]
if sys.platform == "darwin" and sys.version_info[:2] == (3, 14):
    sys.modules["_ruamel_yaml"] = None

from hermes_codex_learning.publication_guardian import MAX_INPUT_BYTES, native_verdict

payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 4097)
if len(payload) > MAX_INPUT_BYTES + 4096:
    raise ValueError("Sharing review input exceeds its byte limit")
request = json.loads(payload)
print(json.dumps(native_verdict(request)))
