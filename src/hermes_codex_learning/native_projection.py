"""Materialize immutable read-only skill snapshots for Codex and Hermes native discovery."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from .config import Settings
from .policy import published_resource
from .quality import proof_valid
from .store import atomic_json, digest

MANIFEST_NAME = ".hermes-learning-managed-skills.json"
_SKILL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
_VERSION_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


def export_root(settings: Settings) -> Path:
    return settings.state / "published-skills"


def snapshots_root(settings: Settings) -> Path:
    return export_root(settings) / "snapshots"


def catalog_root(settings: Settings) -> Path:
    return export_root(settings) / "catalog"


def _swap_symlink(link_path: Path, target: Path | str) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    temp = link_path.with_name(f".{link_path.name}.{uuid.uuid4().hex}.tmp")
    temp.symlink_to(target, target_is_directory=True)
    temp.replace(link_path)


def _remove_readonly_tree(root: Path) -> None:
    if root.is_symlink() or not root.exists():
        root.unlink(missing_ok=True)
        return
    uid = os.getuid()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        cur_path = Path(current)
        if cur_path.lstat().st_uid == uid:
            cur_path.chmod(0o700)
        for name in files:
            fp = cur_path / name
            if not fp.is_symlink() and fp.lstat().st_uid == uid:
                fp.chmod(0o600)
    shutil.rmtree(root)


def _snapshot_intact(snapshot_dir: Path, files: dict[str, str]) -> bool:
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        return False
    observed = {}
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_symlink():
            return False
        if path.is_file():
            rel = str(path.relative_to(snapshot_dir))
            observed[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return observed == files and "SKILL.md" in observed


def materialize_snapshot(snapshots_dir: Path, version: str, source_root: Path, files: dict[str, str]) -> Path:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("Invalid skill artifact version")
    target = snapshots_dir / version
    if _snapshot_intact(target, files):
        return target
    if target.exists() or target.is_symlink():
        _remove_readonly_tree(target)
    snapshots_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    source_resolved = source_root.resolve()
    stage = Path(tempfile.mkdtemp(prefix=f".snap-{version[:12]}-", dir=snapshots_dir))
    try:
        stage.chmod(0o755)
        for relative, expected_sha in sorted(files.items()):
            src = source_resolved / relative
            if src.is_symlink() or not src.resolve().is_relative_to(source_resolved) or not src.is_file():
                raise ValueError("Published resource escapes its source snapshot")
            raw = src.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise ValueError("Published resource checksum mismatch")
            dst = stage / relative
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if not dst.parent.resolve().is_relative_to(stage.resolve()):
                raise ValueError("Published resource path escapes stage")
            dst.write_bytes(raw)
            dst.chmod(0o444)
        for current, dirs, _ in os.walk(stage, topdown=False, followlinks=False):
            cur = Path(current)
            if cur != stage:
                cur.chmod(0o555)
        stage.replace(target)
        target.chmod(0o555)
    except BaseException:
        if stage.exists():
            _remove_readonly_tree(stage)
        raise
    return target


def eligible_publications(settings: Settings, store) -> dict[str, dict]:
    from .enterprise import MODE, boundary, is_trusted_local
    trusted_local = is_trusted_local(settings.data_policy)
    active = boundary(settings.data_policy)
    if settings.data_policy.get("mode") == MODE and not active:
        return {}
    active_id = active["id"] if active else None
    publications = store.get("publications-v1", {})
    if not isinstance(publications, dict):
        return {}
    attestations = store.get("semantic-skill-attestations", {})
    withheld = set()
    revoked = set()
    try:
        withheld = {(row[0], row[1]) for row in store.db.execute("SELECT name,version FROM withheld_versions")}
    except sqlite3.OperationalError:
        pass
    try:
        revoked = {row[0] for row in store.db.execute("SELECT name FROM shared_skill_removals")}
    except sqlite3.OperationalError:
        pass

    eligible: dict[str, dict] = {}
    for name, record in sorted(publications.items()):
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name) or name.startswith(".") or name in revoked:
            continue
        if not isinstance(record, dict):
            continue
        check = record.get("check", {})
        audience = check.get("audience", "public")
        required = check.get("boundary_id") or attestations.get(name, {}).get("boundary_id")
        if not trusted_local and (audience == "enterprise" or required):
            if not active_id or required != active_id:
                continue
        artifact = record.get("artifact", {})
        version = artifact.get("version")
        files = artifact.get("files", {})
        resource_dir = artifact.get("resource_dir")
        if (not isinstance(version, str) or not _VERSION_RE.fullmatch(version)
                or not isinstance(files, dict) or "SKILL.md" not in files
                or not isinstance(resource_dir, str) or not Path(resource_dir).is_dir()):
            continue
        if (name, version) in withheld:
            continue
        if settings.quality_checks:
            proof = store.get("quality:" + digest([name, version]), {})
            try:
                if any(Path(rel).suffix in {".py", ".sh"}
                       and not proof_valid(published_resource(record, rel), proof) for rel in files):
                    continue
            except (OSError, ValueError):
                continue
        eligible[name] = {
            "name": name,
            "version": version,
            "files": files,
            "resource_dir": Path(resource_dir),
            "audience": "enterprise" if (audience == "enterprise" or required) else "public",
            "boundary_id": required,
        }
    return eligible


def _load_manifest(skills_dir: Path) -> dict:
    manifest_path = skills_dir / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_managed_symlink(entry: Path, name: str, manifest: dict, settings: Settings) -> bool:
    if not entry.is_symlink():
        return False
    owned_manifest = (manifest.get("schema") == 1 and manifest.get("owner") == "hermes-learning"
                      and manifest.get("home") == str(settings.home.resolve()))
    if owned_manifest and name in (manifest.get("skills") or {}):
        return True
    try:
        raw_target = Path(os.readlink(entry))
        resolved = (entry.parent / raw_target).resolve() if not raw_target.is_absolute() else raw_target.resolve()
        return resolved.is_relative_to(export_root(settings).resolve())
    except OSError:
        return False


def sync_published_skills(settings: Settings, store, *, dry_run: bool = False) -> dict:
    enabled = bool(settings.native_skill_projection and settings.checked_publication)
    eligible = eligible_publications(settings, store) if enabled else {}
    root = export_root(settings)
    snaps = snapshots_dir = snapshots_root(settings)
    cat = catalog_root(settings)
    projected: dict[str, str] = {}
    shadowed: dict[str, str] = {}
    removed: list[str] = []

    if dry_run:
        if settings.codex_skills_dir is not None and settings.codex_skills_dir.is_dir():
            manifest = _load_manifest(settings.codex_skills_dir)
            manifest_home = manifest.get("home")
            foreign_owner = (
                manifest.get("owner") == "hermes-learning"
                and isinstance(manifest_home, str)
                and manifest_home != str(settings.home.resolve())
                and Path(manifest_home).is_dir()
            )
            for name, info in eligible.items():
                dest = settings.codex_skills_dir / name
                if foreign_owner:
                    shadowed[name] = "owned_by_other_learning_home"
                elif (dest.exists() or dest.is_symlink()) and not _is_managed_symlink(dest, name, manifest, settings):
                    shadowed[name] = "unmanaged_host_entry"
                else:
                    projected[name] = info["version"]
            if manifest.get("owner") == "hermes-learning" and manifest.get("home") == str(settings.home.resolve()):
                removed = sorted(set(manifest.get("skills") or {}) - set(eligible))
        else:
            projected = {name: info["version"] for name, info in eligible.items()}
        return {
            "dry_run": True,
            "enabled": enabled,
            "catalog_dir": str(cat),
            "codex_skills_dir": str(settings.codex_skills_dir) if settings.codex_skills_dir else None,
            "projected_skills": sorted(projected),
            "versions": projected,
            "shadowed": shadowed,
            "removed": removed,
        }

    materialized: dict[str, Path] = {}
    if enabled and eligible:
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        snapshots_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        cat.mkdir(parents=True, exist_ok=True, mode=0o755)
        for name, info in list(eligible.items()):
            try:
                snap = materialize_snapshot(snapshots_dir, info["version"], info["resource_dir"], info["files"])
                materialized[name] = snap
            except (OSError, ValueError):
                eligible.pop(name, None)

    if cat.is_dir() and not cat.is_symlink():
        for entry in sorted(cat.iterdir()):
            if entry.name not in materialized:
                if entry.is_symlink():
                    entry.unlink(missing_ok=True)
                elif entry.is_dir():
                    _remove_readonly_tree(entry)
        for name, snap in sorted(materialized.items()):
            rel_target = Path("..") / "snapshots" / snap.name
            link = cat / name
            if not link.is_symlink() or Path(os.readlink(link)) != rel_target:
                _swap_symlink(link, rel_target)
    elif materialized:
        cat.mkdir(parents=True, exist_ok=True, mode=0o755)
        for name, snap in sorted(materialized.items()):
            _swap_symlink(cat / name, Path("..") / "snapshots" / snap.name)

    codex_projected: dict[str, dict] = {}
    if settings.codex_skills_dir is not None:
        skills_dir = settings.codex_skills_dir
        if materialized:
            skills_dir.mkdir(parents=True, exist_ok=True)
        if skills_dir.is_dir() and not skills_dir.is_symlink():
            manifest = _load_manifest(skills_dir)
            previous_skills = manifest.get("skills") if isinstance(manifest.get("skills"), dict) else {}
            manifest_home = manifest.get("home")
            same_owner = (manifest.get("owner") == "hermes-learning"
                          and manifest_home == str(settings.home.resolve()))
            foreign_owner = (
                manifest.get("owner") == "hermes-learning"
                and not same_owner
                and isinstance(manifest_home, str)
                and Path(manifest_home).is_dir()
            )
            if foreign_owner:
                for name in sorted(materialized):
                    shadowed[name] = "owned_by_other_learning_home"
                candidates_to_prune = set()
            else:
                candidates_to_prune = set(previous_skills) - set(materialized) if same_owner else set()
                for entry in skills_dir.iterdir():
                    if entry.name not in materialized and _is_managed_symlink(entry, entry.name, manifest, settings):
                        candidates_to_prune.add(entry.name)
            for stale_name in sorted(candidates_to_prune):
                stale_entry = skills_dir / stale_name
                if _is_managed_symlink(stale_entry, stale_name, manifest, settings):
                    stale_entry.unlink(missing_ok=True)
                    removed.append(stale_name)

            if not foreign_owner:
                for name, snap in sorted(materialized.items()):
                    dest = skills_dir / name
                    if (dest.exists() or dest.is_symlink()) and not _is_managed_symlink(dest, name, manifest, settings):
                        shadowed[name] = "unmanaged_host_entry"
                        continue
                    target = snap.resolve()
                    if not dest.is_symlink() or Path(os.readlink(dest)) != target:
                        _swap_symlink(dest, target)
                    codex_projected[name] = {
                        "version": eligible[name]["version"],
                        "snapshot": str(target),
                        "audience": eligible[name]["audience"],
                    }
                    projected[name] = eligible[name]["version"]

                manifest_path = skills_dir / MANIFEST_NAME
                if codex_projected:
                    atomic_json(manifest_path, {
                        "schema": 1,
                        "owner": "hermes-learning",
                        "home": str(settings.home.resolve()),
                        "catalog_dir": str(cat.resolve()) if cat.exists() else str(cat),
                        "updated_at": time.time(),
                        "skills": codex_projected,
                    })
                elif same_owner and manifest_path.is_file() and not manifest_path.is_symlink():
                    manifest_path.unlink(missing_ok=True)
    else:
        projected = {name: eligible[name]["version"] for name in sorted(materialized)}

    active_versions = {eligible[name]["version"] for name in materialized}
    if snaps.is_dir() and not snaps.is_symlink():
        for child in sorted(snaps.iterdir()):
            if child.name.startswith("."):
                continue
            if child.name not in active_versions:
                _remove_readonly_tree(child)

    summary = {
        "enabled": enabled,
        "catalog_dir": str(cat),
        "codex_skills_dir": str(settings.codex_skills_dir) if settings.codex_skills_dir else None,
        "projected_skills": sorted(projected),
        "catalog_skills": sorted(materialized),
        "versions": {name: eligible[name]["version"] for name in sorted(materialized)},
        "shadowed": shadowed,
        "removed": removed,
        "updated_at": time.time(),
    }
    if hasattr(store, "set"):
        try:
            store.set("native-skill-projection", summary)
        except sqlite3.OperationalError:
            pass
    return summary


def projection_status(settings: Settings, store) -> dict:
    recorded = store.get("native-skill-projection")
    cat = catalog_root(settings)
    live_codex = []
    if settings.codex_skills_dir and settings.codex_skills_dir.is_dir():
        manifest = _load_manifest(settings.codex_skills_dir)
        for name in sorted((manifest.get("skills") or {}).keys()):
            entry = settings.codex_skills_dir / name
            if _is_managed_symlink(entry, name, manifest, settings) and entry.exists():
                live_codex.append(name)
    return {
        **(recorded or {
            "enabled": bool(settings.native_skill_projection and settings.checked_publication),
            "catalog_dir": str(cat),
            "codex_skills_dir": str(settings.codex_skills_dir) if settings.codex_skills_dir else None,
            "projected_skills": [],
            "catalog_skills": [],
            "versions": {},
            "shadowed": {},
        }),
        "catalog_exists": cat.is_dir(),
        "live_codex_symlinks": live_codex,
    }


def configure_hermes_external_dir(settings: Settings, profile_home: Path, *, apply: bool = False) -> dict:
    profile_resolved = profile_home.expanduser().resolve()
    if profile_resolved == settings.home.resolve():
        raise ValueError("Do not mount published external_dirs back into the learning owner home")
    config_path = profile_resolved / "config.yaml"
    if not config_path.is_file() or config_path.is_symlink():
        raise FileNotFoundError(f"Hermes config.yaml not found at {config_path}")
    target_dir = str(catalog_root(settings).resolve())
    backup_dir = profile_resolved / "backups"
    sys_path_added = False
    if str(settings.source) not in os.sys.path and settings.source.is_dir():
        os.sys.path.insert(0, str(settings.source))
        sys_path_added = True
    try:
        try:
            import hermes_yaml as yaml
            from hermes_cli.config import atomic_config_write
        except ImportError:
            import subprocess
            if not settings.interpreter.is_file():
                raise
            helper = (
                "import json, pathlib, shutil, sys, time\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "import hermes_yaml as yaml\n"
                "from hermes_cli.config import atomic_config_write\n"
                "config_path = pathlib.Path(sys.argv[2])\n"
                "target_dir = sys.argv[3]\n"
                "apply = sys.argv[4] == '1'\n"
                "parsed = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}\n"
                "if not isinstance(parsed, dict): raise ValueError('Hermes config.yaml root must be a mapping')\n"
                "skills_cfg = dict(parsed.get('skills') or {})\n"
                "existing = list(skills_cfg.get('external_dirs') or [])\n"
                "if not isinstance(existing, list) or any(not isinstance(x, str) for x in existing):\n"
                "    raise ValueError('skills.external_dirs must be a list of path strings')\n"
                "already = any(pathlib.Path(x).expanduser().resolve() == pathlib.Path(target_dir) for x in existing if x)\n"
                "updated_dirs = existing if already else [*existing, target_dir]\n"
                "out = {'profile': str(config_path.parent), 'config': str(config_path), 'catalog_dir': target_dir,\n"
                "       'already_configured': already, 'external_dirs': updated_dirs, 'applied': False, 'backup': None}\n"
                "if apply and not already:\n"
                "    bdir = config_path.parent / 'backups'\n"
                "    bdir.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
                "    bpath = bdir / f'config.yaml.pre-hermes-projection-{int(time.time())}'\n"
                "    shutil.copy2(config_path, bpath); bpath.chmod(0o600)\n"
                "    atomic_config_write(config_path, {**parsed, 'skills': {**skills_cfg, 'external_dirs': updated_dirs}})\n"
                "    out.update(applied=True, backup=str(bpath))\n"
                "print(json.dumps(out, ensure_ascii=False))\n"
            )
            proc = subprocess.run(
                [str(settings.interpreter), "-I", "-B", "-c", helper,
                 str(settings.source), str(config_path), target_dir, "1" if apply else "0"],
                capture_output=True, text=True, check=True, timeout=15,
            )
            return json.loads(proc.stdout)
        raw = config_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw) or {}
        if not isinstance(parsed, dict):
            raise ValueError("Hermes config.yaml root must be a mapping")
        skills_cfg = dict(parsed.get("skills") or {})
        existing = list(skills_cfg.get("external_dirs") or [])
        if not isinstance(existing, list) or any(not isinstance(item, str) for item in existing):
            raise ValueError("skills.external_dirs must be a list of path strings")
        already_present = any(Path(item).expanduser().resolve() == Path(target_dir) for item in existing if item)
        updated_dirs = existing if already_present else [*existing, target_dir]
        result = {
            "profile": str(profile_resolved),
            "config": str(config_path),
            "catalog_dir": target_dir,
            "already_configured": already_present,
            "external_dirs": updated_dirs,
            "applied": False,
            "backup": None,
        }
        if not apply or already_present:
            return result
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path = backup_dir / f"config.yaml.pre-hermes-projection-{int(time.time())}"
        shutil.copy2(config_path, backup_path)
        backup_path.chmod(0o600)
        updated_cfg = {**parsed, "skills": {**skills_cfg, "external_dirs": updated_dirs}}
        atomic_config_write(config_path, updated_cfg)
        return {**result, "applied": True, "backup": str(backup_path)}
    finally:
        if sys_path_added:
            try:
                os.sys.path.remove(str(settings.source))
            except ValueError:
                pass
