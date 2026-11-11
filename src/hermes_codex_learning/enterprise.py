"""Operator-authorized enterprise scope, bound to this host and model configuration.

This is a deployment authorization, not a general DLP classifier or public release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from pathlib import Path

from .store import digest, redact

MODE = "trusted_enterprise"
AUTHORITY = "operator_confirmed_enterprise_deployment"


def boundary(policy):
    if policy.get("mode") != MODE:
        return None
    try:
        path=Path(policy["enterprise_boundary"])
        if not path.is_absolute() or path.is_symlink() or path.stat().st_uid!=os.getuid() or path.stat().st_size>16384:
            return None
        record=json.loads(path.read_text())
        config=Path(record["model_config"])
        if (record.get("schema_version")!=1 or record.get("authority")!=AUTHORITY
                or record.get("hostname")!=socket.gethostname() or record.get("uid")!=os.getuid()
                or record.get("body_learning") is not True or not config.is_absolute()
                or config.is_symlink() or config.stat().st_uid!=os.getuid()
                or hashlib.sha256(config.read_bytes()).hexdigest()!=record["model_config_sha256"]):
            return None
        identity=digest(["enterprise",record["hostname"],record["uid"],str(config.parent.resolve())])
        return {**record,"id":identity,"policy_version":"enterprise-v1:"+digest(record)}
    except (OSError,ValueError,KeyError,TypeError):
        return None


def make_boundary(settings):
    config=(settings.home/"config.yaml").resolve()
    return {"schema_version":1,"authority":AUTHORITY,"hostname":socket.gethostname(),"uid":os.getuid(),
            "model_config":str(config),"model_config_sha256":hashlib.sha256(config.read_bytes()).hexdigest(),
            "body_learning":True,"internal_sharing":True,"approved_at":time.time()}


def explicitly_restricted(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {"classification","data_classification","sensitivity","secure_label"} and item=="L4":
                return True
            if key in {"do_not_learn","external_processing_forbidden"} and item is True:
                return True
            if explicitly_restricted(item):
                return True
    elif isinstance(value,list):
        return any(explicitly_restricted(item) for item in value)
    elif isinstance(value,str):
        return bool(re.search(r"(?im)^\s*(?:密级|classification|sensitivity)\s*[:：]\s*L4\b",value))
    return False


def scrub(value):
    value=redact(value)
    if isinstance(value,dict):
        return {key:"[REDACTED]" if re.fullmatch(r"(?i)(?:set[-_])?cookie",key) else scrub(item)
                for key,item in value.items()}
    if isinstance(value,list):
        return [scrub(item) for item in value]
    if isinstance(value,str):
        value=re.sub(r"(?im)^(\s*(?:cookie|set-cookie|authorization)\s*:\s*)[^\r\n]+",
                     r"\1[REDACTED]",value)
        value=re.sub(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----.*?-----END (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----",
                     "[REDACTED PRIVATE KEY]",value,flags=re.S)
        value=re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b","[REDACTED TOKEN]",value)
    return value


def assess(payload, policy, purpose):
    approved=boundary(policy)
    if not approved:
        return {"allowed":False,"reason":"enterprise_boundary_unavailable_or_changed"}
    if explicitly_restricted(payload):
        return {"allowed":False,"reason":"explicitly_restricted_material"}
    safe=scrub(payload)
    from .assessment import IDENTITY_FIELDS
    if any(safe.get(key)!=payload.get(key) for key in IDENTITY_FIELDS):
        return {"allowed":False,"reason":"credential_in_source_identity"}
    if purpose=="share" and approved.get("internal_sharing") is not True:
        return {"allowed":False,"reason":"enterprise_sharing_not_authorized"}
    return {"allowed":True,"schema_version":2,"classification":"approved","audience":"enterprise",
            "boundary_id":approved["id"],"policy_version":approved["policy_version"],
            "assessment_id":digest([approved["id"],purpose,digest(payload)]),
            "input_hash":digest(payload),"output_hash":digest(safe),"payload":safe,
            "body_retained":True,"credentials_removed":safe!=payload,
            "source_refs":["enterprise-boundary:"+approved["id"]],
            "sharing_allowed":approved.get("internal_sharing") is True,
            "basis":"operator_authorized_enterprise_deployment"}


def scope_view(context, policy):
    approved=boundary(policy)
    return {**context,"active_enterprise_boundary":approved["id"] if approved else None}


def requires_scoping(context, policy):
    return bool(policy.get("mode")==MODE or context.get("enterprise_memory_boundaries")
                or context.get("enterprise_skill_boundaries")
                or any(item.get("audience")=="enterprise" for item in context.get("publications",{}).values()))


def scope_boundary(store, handle):
    return (store.get("enterprise-scope:"+handle)
            or store.get("assessment:"+handle,{}).get("boundary_id"))
