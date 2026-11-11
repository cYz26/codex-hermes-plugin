"""Use the configured native pending store in a dedicated, non-model process."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--home", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(args.source.resolve()))

try:
    from hermes_constants import get_hermes_home
    from tools.memory_tool import MemoryStore, apply_memory_pending, load_on_disk_store
    from tools.memory_tool_store import _patch_entry
    from tools import write_approval
    from hermes_codex_learning.approvals import ID_PATTERN, inspect_record
    from hermes_codex_learning.store import digest

    home = get_hermes_home().resolve()
    if home != args.home.resolve():
        raise ValueError("Native approval home does not match the configured learning home")
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError("Approval request exceeds 64 KiB")
    request = json.loads(raw)
    action = request.get("action")
    if action not in {"inspect", "approve", "reject"}:
        raise ValueError("Unsupported native approval action")
    entries = MemoryStore._read_file(home / "memories/MEMORY.md")
    result = []
    identifiers = request.get("ids", []) if action == "inspect" else [request.get("id")]
    for identifier in identifiers:
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
            raise ValueError("Invalid native pending identifier")
        path = home / "pending/memory" / (identifier + ".json")
        if path.is_symlink() or not path.resolve().is_relative_to(home / "pending/memory"):
            raise ValueError("Pending proposal escapes the native memory queue")
        if path.is_file():
            if path.stat().st_size > 65536:
                raise ValueError("Native pending proposal exceeds 64 KiB")
            record = write_approval.get_pending(write_approval.MEMORY, identifier)
            if record is None or record.get("id") != identifier:
                raise ValueError("Native pending proposal is invalid")
            result.append(inspect_record(record, entries, patcher=_patch_entry))
    if action != "inspect":
        if len(result) != 1:
            raise ValueError("Native pending proposal no longer exists")
        item = result[0]
        if digest(item["record"]) != request.get("revision"):
            raise ValueError("Native pending proposal changed after review")
        if action == "reject":
            removed = write_approval.discard_pending(write_approval.MEMORY, identifiers[0])
            result = {"state": "rejected" if removed else "not_found", "applied": False,
                      "pending_removed": removed}
        else:
            if item["state"] != "pending":
                raise ValueError("Native pending proposal is stale or unsupported; refresh the review")
            outcome = apply_memory_pending(item["record"]["payload"], load_on_disk_store())
            if outcome.get("success") is not True:
                # Native budget errors can contain the entire memory inventory.
                result = {"state": "failed", "applied": False, "pending_removed": False,
                          "reason": "原生记忆写入拒绝，提案已保留"}
            else:
                after_entries = MemoryStore._read_file(home / "memories/MEMORY.md")
                expected = {change["after"].strip() for change in item["changes"] if change["after"].strip() in after_entries}
                from hermes_codex_learning.knowledge import memory_entries
                raw_context = {"memories": {"MEMORY.md": (home / "memories/MEMORY.md").read_text(encoding="utf-8-sig")}}
                written = [entry for _, _, entry in memory_entries(raw_context) if entry.strip() in expected]
                try:
                    write_approval.discard_pending(write_approval.MEMORY, identifiers[0])
                    removed = not (home / "pending/memory" / (identifiers[0] + ".json")).exists()
                except OSError:
                    removed = False
                result = {"state": "approved" if removed else "approved_pending_cleanup", "applied": True,
                          "pending_removed": removed, "written_entries": written}
    print(json.dumps(result, ensure_ascii=False))
except Exception as error:
    print(json.dumps({"error": str(error), "error_type": type(error).__name__}, ensure_ascii=False))
    raise SystemExit(2)
