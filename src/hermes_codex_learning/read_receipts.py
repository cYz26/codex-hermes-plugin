"""Stateless read receipts; persistence starts only after a host delivery event."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .store import dumps

PREFIX = "read-v1."


def initialize(state: Path):
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=state/"delivery-signing-key"
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        key(state)
        return
    with os.fdopen(fd,"wb") as stream:
        stream.write(secrets.token_bytes(32));stream.flush();os.fsync(stream.fileno())


def key(state: Path):
    path=state/"delivery-signing-key"
    if path.is_symlink() or path.stat().st_uid!=os.getuid():
        raise PermissionError("Invalid read-receipt key")
    value=path.read_bytes()
    if len(value)!=32:
        raise PermissionError("Read-receipt key is unavailable")
    return value


def issue(state, value, project):
    payload={name:value[name] for name in ("name","version","file_path","content_hash")}
    payload.update(project=project,scope=value.get("scope","worktree"),expires_at=int(time.time())+30*86400,nonce=secrets.token_hex(16))
    encoded=base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")
    signature=hmac.new(key(state),encoded.encode(),hashlib.sha256).hexdigest()
    return PREFIX+encoded+"."+signature


def verify(state, token, project):
    try:
        if not isinstance(token,str) or not token.startswith(PREFIX) or len(token)>4096:
            raise ValueError()
        encoded,signature=token[len(PREFIX):].split(".")
        expected=hmac.new(key(state),encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):
            raise ValueError()
        value=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
        if value["expires_at"]<time.time() or (value["project"]!=project and not(value["project"] is None and value["scope"]=="global")):
            raise ValueError()
        return value
    except (OSError,ValueError,KeyError,TypeError):
        raise PermissionError("Read receipt is invalid, expired or belongs to another project") from None
