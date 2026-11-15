"""Use additive Learning API capabilities while retaining old read-only clients."""


def skill(api, **kwargs):
    try:
        return api.read(kind="skill", issue_receipt=False, **kwargs)
    except TypeError:
        return api.read(kind="skill", **kwargs)


def status(api):
    try:
        return api.read(kind="status", detail="summary")
    except TypeError:
        return api.read(kind="status")


def reviewed_revision(api, handle):
    # Learning API v1 exposes the watermark through its local owner, but not
    # through the bounded status view. Read it under that owner's lock.
    service = getattr(api, "_service", api)
    db = getattr(service, "db", None)
    lock = getattr(service, "lock", None)
    if db is None or lock is None:
        return None
    with lock:
        row = db.execute("SELECT reviewed FROM scopes WHERE id=?", (handle,)).fetchone()
    return int(row[0]) if row else None


def delivered_snapshot(api, name, version, file_path=None):
    try:
        result = api.read(kind="skill",name=name,file_path=file_path,artifact_version=version)
        return result if result.get("version")==version and result.get("delivery_token") else None
    except TypeError:
        return None
