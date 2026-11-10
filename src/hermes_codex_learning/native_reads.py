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


def delivered_snapshot(api, name, version, file_path=None):
    try:
        result = api.read(kind="skill",name=name,file_path=file_path,artifact_version=version)
        return result if result.get("version")==version and result.get("delivery_token") else None
    except TypeError:
        return None
