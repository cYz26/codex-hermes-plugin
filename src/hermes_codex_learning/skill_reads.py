"""Read only previously prepared artifacts; no native execution or persistence."""
from __future__ import annotations

from pathlib import Path

from .policy import published_resource
from .quality import proof_valid
from .read_receipts import issue
from .store import ReadStore, digest


def load(settings, name, project, file_path=None):
    store=ReadStore(settings.state)
    try:
        name=store.get("publication-aliases",{}).get(name,name)
        origins=store.get("knowledge-index-v1",{}).get("skills",{}).get(name,{}).get("projects",[])
        publication=store.get("publications-v1",{}).get(name)
        shared=settings.checked_publication and origins!=[project]
        if shared:
            if not publication:
                raise PermissionError("Skill has not passed shared publication checks")
            record=publication
        else:
            record=store.get("artifact-read-cache",{}).get(name)
            if not record:
                raise RuntimeError("Skill snapshot is not ready; retry after the next background publication")
        value=published_resource(record,file_path)
        if store.db.execute("SELECT 1 FROM withheld_versions WHERE name=? AND version=?",(name,value["version"])).fetchone():
            if publication and publication["artifact"]["version"]!=value["version"]:
                value=published_resource(publication,file_path);shared=True
            else:
                raise PermissionError("This skill version has a failed host validation")
        proof=store.get("quality:"+digest([name,value["version"]]),{})
        if settings.quality_checks and Path(value["file_path"]).suffix in {".py",".sh"} and not proof_valid(value,proof):
            raise PermissionError("Executable resource has not passed independent validation")
        value.update(scope="global" if shared else "worktree",request_project=project,
                     source_projects=[] if shared else origins,
                     validation=proof or {"state":"pending","business_effectiveness":"not_inferred"})
        if shared:
            value["source_ids"]=[digest(item) for item in record.get("sources",[])]
        value["delivery_token"]=issue(settings.state,value,project)
        return value
    finally:
        store.close()
