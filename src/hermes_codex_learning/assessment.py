"""Source-bound public/synthetic evidence. This is not a general data classifier."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .store import atomic_json, atomic_text, digest, redact

IDENTITY_FIELDS = ("hook_event_name", "session_id", "turn_id", "tool_use_id", "cwd", "transcript_path", "source")
LIMIT = 2 * 1024 * 1024
NEUTRAL = "Review only the assessed evidence below. Unassessed task prose was withheld."


def load_sources(path: Path) -> tuple[list, str]:
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_size > LIMIT:
        raise ValueError("Source manifest is not a bounded, owned regular file")
    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1 or len(manifest.get("sources", [])) > 100:
        raise ValueError("Unsupported source manifest")
    sources = []
    for item in manifest.get("sources", []):
        source = Path(item["path"])
        if (not source.is_absolute() or source.is_symlink() or not source.is_file()
                or source.stat().st_size > LIMIT or source.stat().st_uid != os.getuid()):
            raise ValueError("Source is unavailable")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Source content changed")
        if item.get("classification") not in {"public", "synthetic"}:
            raise ValueError("Source classification is not supported")
        if item["classification"] == "public":
            public_url(item["reference"])
        text = raw.decode("utf-8")
        if redact(text) != text:
            raise ValueError("Source contains credential material")
        sources.append((text, item))
    return sources, digest(manifest)


def public_url(url: str) -> str:
    value = urlsplit(url)
    if (value.scheme != "https" or value.netloc != "raw.githubusercontent.com" or value.query or value.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[a-f0-9]{40}/[^\s]+", value.path)
            or ".." in unquote(value.path).split("/")):
        raise ValueError("Use an unauthenticated raw GitHub URL pinned to an exact commit")
    return url


def register_source(manifest: Path, *, url: str | None = None, synthetic: Path | None = None) -> dict:
    if bool(url) == bool(synthetic):
        raise ValueError("Choose one public URL or explicit synthetic fixture")
    if url:
        request = urllib.request.Request(public_url(url), headers={"User-Agent":"hermes-learning-source-check"})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.geturl() != url:
                raise ValueError("Public source redirected")
            raw = response.read(LIMIT + 1)
        reference, classification = url, "public"
    else:
        raw = synthetic.read_bytes()
        reference, classification = "synthetic:" + synthetic.name, "synthetic"
    if len(raw) > LIMIT or redact(raw.decode("utf-8")) != raw.decode("utf-8"):
        raise ValueError("Source is oversized or contains credential material")
    checksum = hashlib.sha256(raw).hexdigest()
    source = manifest.parent / "sources" / (checksum + ".txt")
    atomic_text(source, raw.decode("utf-8"))
    data = json.loads(manifest.read_text()) if manifest.exists() else {"schema_version":1, "sources":[]}
    item = {"path":str(source.absolute()), "sha256":checksum, "classification":classification, "reference":reference}
    data["sources"] = [entry for entry in data["sources"] if entry["reference"] != reference] + [item]
    atomic_json(manifest, data)
    return {"reference":reference, "sha256":checksum, "manifest":str(manifest)}


def assess(payload: dict, manifest: Path) -> dict:
    sources, policy_version = load_sources(manifest)
    refs = set()
    def fragments(value):
        found = []
        def visit(value):
            if isinstance(value, str):
                for public, item in sources:
                    matched = value if len(value) >= 32 and value in public else public if public and public in value else None
                    if matched and matched not in found:
                        found.append(matched[:65536])
                        refs.add(item["reference"])
            elif isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(value)
        return "\n\n".join(found)[:65536]
    safe = {key: payload[key] for key in IDENTITY_FIELDS if key in payload}
    if "hook_event_name" in payload:
        event = payload["hook_event_name"]
        if event == "UserPromptSubmit":
            safe["prompt"] = fragments(payload.get("prompt"))
        elif event == "Stop":
            safe["last_assistant_message"] = fragments(payload.get("last_assistant_message"))
        elif event in {"PreToolUse", "PostToolUse"}:
            safe.update(tool_name=payload.get("tool_name", "unknown"), tool_input={})
            output = fragments(payload.get("tool_response"))
            safe["tool_response"] = {"output":output}
            response = payload.get("tool_response")
            if isinstance(response, dict):
                from .normalize import _execution_result
                response, _ = _execution_result(response)
            if isinstance(response, dict):
                for key in ("exit_code", "session_id", "cell_id", "interrupted", "isError"):
                    if isinstance(response.get(key), (int, bool, str)):
                        safe["tool_response"][key] = response[key]
                if response.get("error"):
                    safe["tool_response"]["error"] = "execution_error"
    else:
        safe.update(user=fragments(payload.get("user")) or NEUTRAL,
                    assistant=fragments(payload.get("assistant")) or "",
                    status=payload.get("status", "completed"), coverage=payload.get("coverage", []), tools=[])
        for tool in payload.get("tools", []):
            clean = {key:tool[key] for key in ("call_id", "name", "status", "process") if key in tool}
            clean.update(arguments={}, result={"output":fragments(tool.get("result"))})
            if isinstance(tool.get("result"), dict) and type(tool["result"].get("exit_code")) is int:
                clean["result"]["exit_code"] = tool["result"]["exit_code"]
            safe["tools"].append(clean)
    if not refs:
        return {"allowed":False, "reason":"no_assessed_source_fragment"}
    classification = "public" if all(item["classification"] == "public" for _, item in sources if item["reference"] in refs) else "synthetic"
    return {"schema_version":2, "allowed":True, "classification":classification, "assessment_id":digest([digest(payload), policy_version]),
            "input_hash":digest(payload), "output_hash":digest(safe), "payload":safe,
            "source_refs":sorted(refs), "policy_version":policy_version, "sharing_allowed":classification == "public"}


def validate_replacement(payload: dict, assessment: dict) -> dict:
    safe = assessment.get("payload")
    if (not isinstance(safe, dict) or assessment.get("input_hash") != digest(payload)
            or assessment.get("output_hash") != digest(safe)
            or not isinstance(assessment.get("policy_version"),str) or not 1 <= len(assessment["policy_version"]) <= 128
            or redact(safe) != safe or any(safe.get(key) != payload.get(key) for key in IDENTITY_FIELDS)
            or any(key.startswith("bridge_") for key in safe)):
        raise ValueError("Assessment replacement is not bound to this event")
    references = assessment.get("source_refs",[])
    if not isinstance(references,list) or len(references)>100 or any(not isinstance(item,str) or len(item)>2048 for item in references):
        raise ValueError("Invalid assessment source references")
    if payload.get("tool_name") != safe.get("tool_name"):
        raise ValueError("Assessment cannot change the host tool identity")
    if isinstance(payload.get("tool_response"),dict) and isinstance(safe.get("tool_response"),dict):
        from .normalize import _execution_result
        original,_ = _execution_result(payload["tool_response"])
        replacement,_ = _execution_result(safe["tool_response"])
        if isinstance(original,dict) and isinstance(replacement,dict):
            for key in ("exit_code","session_id","cell_id","interrupted","isError"):
                if key in original and (type(original[key]) is not type(replacement.get(key)) or original[key] != replacement.get(key)):
                    raise ValueError("Assessment cannot alter host execution facts")
    return safe
