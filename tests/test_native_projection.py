import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_learning.capture import hook
from hermes_codex_learning.config import Settings
from hermes_codex_learning.daemon import Bridge
from hermes_codex_learning.enterprise import make_boundary
from hermes_codex_learning.knowledge import context_view
from hermes_codex_learning.native_projection import (
    MANIFEST_NAME,
    catalog_root,
    configure_hermes_external_dir,
    snapshots_root,
    sync_published_skills,
)
from hermes_codex_learning.store import Store, atomic_json, digest


def _write_artifact(base: Path, name: str, body: str, extra: dict[str, str] | None = None) -> tuple[str, dict]:
    files_content = {"SKILL.md": f"---\nname: {name}\ndescription: Fixture {name}\nlearning_scope: global_candidate\n---\n\n{body}\n"}
    if extra:
        files_content.update(extra)
    manifest = {rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in sorted(files_content.items())}
    version = digest(manifest)
    art_dir = base / "learning" / "artifacts" / version
    art_dir.mkdir(parents=True, exist_ok=True)
    for rel, text in files_content.items():
        dst = art_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
    record = {
        "catalog_entry": {"name": name, "description": f"Fixture {name}", "version": version},
        "artifact": {
            "name": name,
            "version": version,
            "resource_dir": str(art_dir),
            "files": manifest,
        },
        "check": {"allowed": True, "version": version},
        "sources": ["/project-a"],
    }
    return version, record


class NativeProjectionTests(unittest.TestCase):
    def setUp(self):
        parent = Path("/var/tmp")
        neutral = parent.is_dir() and not any((item / ".git").exists() for item in (parent, *parent.parents))
        self.temp = tempfile.TemporaryDirectory(dir=str(parent) if neutral else None)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "pilot"
        self.project = self.root / "workspace-a"
        self.other = self.root / "workspace-b"
        self.codex_skills = self.root / "agents-skills"
        for folder in (self.home, self.project, self.other, self.codex_skills):
            folder.mkdir(parents=True, exist_ok=True)
        (self.project / ".git").mkdir()
        (self.other / ".git").mkdir()
        self.settings = Settings(
            self.home,
            self.project,
            self.root,
            Path("/usr/bin/python3"),
            capture_scope="global",
            checked_publication=True,
            native_skill_projection=True,
            codex_skills_dir=self.codex_skills,
        )
        self.store = Store(self.settings.state)

    def tearDown(self):
        self.store.close()
        for cur, dirs, files in os.walk(self.root, topdown=True, followlinks=False):
            p = Path(cur)
            if not p.is_symlink():
                try:
                    p.chmod(0o700)
                except OSError:
                    pass
            for fname in files:
                fp = p / fname
                if not fp.is_symlink():
                    try:
                        fp.chmod(0o600)
                    except OSError:
                        pass
        self.temp.cleanup()

    def test_readonly_snapshot_projection_upgrades_and_protects_host_skills(self):
        host_owned = self.codex_skills / "arkcli-auth"
        host_owned.mkdir()
        (host_owned / "SKILL.md").write_text("Host owned skill", encoding="utf-8")

        v1, pub_v1 = _write_artifact(self.home, "learned-alpha", "Step 1", {"references/guide.md": "Ref 1"})
        _, pub_collision = _write_artifact(self.home, "arkcli-auth", "Colliding skill")
        self.store.set("publications-v1", {"learned-alpha": pub_v1, "arkcli-auth": pub_collision})

        summary = sync_published_skills(self.settings, self.store)
        self.assertEqual(summary["projected_skills"], ["learned-alpha"])
        self.assertEqual(summary["shadowed"], {"arkcli-auth": "unmanaged_host_entry"})
        self.assertEqual((host_owned / "SKILL.md").read_text(encoding="utf-8"), "Host owned skill")

        codex_link = self.codex_skills / "learned-alpha"
        cat_link = catalog_root(self.settings) / "learned-alpha"
        self.assertTrue(codex_link.is_symlink())
        self.assertTrue(cat_link.is_symlink())
        skill_md = codex_link / "SKILL.md"
        self.assertIn("Step 1", skill_md.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(skill_md.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(codex_link.resolve().stat().st_mode), 0o555)

        v2, pub_v2 = _write_artifact(self.home, "learned-alpha", "Step 2 revised")
        self.store.set("publications-v1", {"learned-alpha": pub_v2})
        upgraded = sync_published_skills(self.settings, self.store)
        self.assertEqual(upgraded["versions"]["learned-alpha"], v2)
        self.assertIn("Step 2 revised", (codex_link / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse((snapshots_root(self.settings) / v1).exists())
        self.assertTrue((self.codex_skills / MANIFEST_NAME).is_file())

    def test_enterprise_boundary_change_immediately_unlinks_enterprise_skills(self):
        (self.home / "config.yaml").write_text("_config_version: 1\nmodel:\n  default: enterprise-a\n", encoding="utf-8")
        boundary_file = self.settings.state / "enterprise-boundary.json"
        record = make_boundary(self.settings)
        atomic_json(boundary_file, record)
        policy = {"enabled": True, "mode": "trusted_enterprise", "enterprise_boundary": str(boundary_file)}
        ent_settings = replace(self.settings, data_policy=policy)
        from hermes_codex_learning.enterprise import boundary
        active_id = boundary(policy)["id"]

        _, ent_pub = _write_artifact(self.home, "learned-internal-sop", "Enterprise internal workflow")
        ent_pub["check"] = {"allowed": True, "audience": "enterprise", "boundary_id": active_id}
        self.store.set("publications-v1", {"learned-internal-sop": ent_pub})

        active_sync = sync_published_skills(ent_settings, self.store)
        self.assertEqual(active_sync["projected_skills"], ["learned-internal-sop"])
        self.assertTrue((self.codex_skills / "learned-internal-sop").is_symlink())

        # Changing model routing invalidates the enterprise boundary and must unlink projections.
        (self.home / "config.yaml").write_text("_config_version: 1\nmodel:\n  default: external-model\n", encoding="utf-8")
        revoked_sync = sync_published_skills(ent_settings, self.store)
        self.assertEqual(revoked_sync["projected_skills"], [])
        self.assertFalse((self.codex_skills / "learned-internal-sop").exists())
        self.assertFalse((self.codex_skills / "learned-internal-sop").is_symlink())
        self.assertFalse((catalog_root(ent_settings) / "learned-internal-sop").exists())

    def test_hook_omits_baseline_projected_skills_and_surfaces_mid_session_deltas(self):
        context = {
            "version": "ctx-1",
            "checked_publication": True,
            "native_projected_skills": ["learned-baseline"],
            "memories": {"MEMORY.md": "Project A invariant: run check_release before packaging", "USER.md": ""},
            "skills": [
                {"name": "learned-baseline", "description": "Baseline architecture comparison", "version": "v1"},
            ],
            "publications": {
                "learned-baseline": {
                    "catalog_entry": {"name": "learned-baseline", "description": "Baseline architecture comparison", "artifact_version": "v1"},
                    "version": "v1",
                    "audience": "public",
                }
            },
            "bridge_provenance": {
                "version": 1,
                "memory": {digest(["MEMORY.md", "Project A invariant: run check_release before packaging"]): {"project": str(self.project)}},
                "skills": {"learned-baseline": {"projects": [str(self.other)]}},
            },
        }
        atomic_json(self.settings.state / "context.json", context)

        start = hook({
            "hook_event_name": "SessionStart",
            "cwd": str(self.project),
            "session_id": "chat-1",
            "turn_id": "turn-1",
        }, self.settings)
        injected = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Project A invariant", injected)
        self.assertIn(str(self.project), injected)
        self.assertNotIn("learned-baseline", injected)

        # Subsequent prompt with identical scoped memory and only baseline skills must not re-inject duplicate context.
        second = hook({
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.project),
            "session_id": "chat-1",
            "turn_id": "turn-1",
            "prompt": "Compare baseline architecture",
        }, self.settings)
        self.assertEqual(second, {})

        # A new skill published mid-session (not in SessionStart baseline) IS surfaced when relevant.
        context["version"] = "ctx-2"
        context["native_projected_skills"] = ["learned-baseline", "learned-mid-session-fix"]
        context["skills"].append({"name": "learned-mid-session-fix", "description": "Repair delimited CSV header parsing", "version": "v2"})
        context["publications"]["learned-mid-session-fix"] = {
            "catalog_entry": {"name": "learned-mid-session-fix", "description": "Repair delimited CSV header parsing", "artifact_version": "v2"},
            "version": "v2",
            "audience": "public",
        }
        context["bridge_provenance"]["skills"]["learned-mid-session-fix"] = {"projects": [str(self.other)]}
        atomic_json(self.settings.state / "context.json", context)

        third = hook({
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.project),
            "session_id": "chat-1",
            "turn_id": "turn-2",
            "prompt": "帮我修复 CSV 表头分隔解析问题",
        }, self.settings)
        delta_text = third["hookSpecificOutput"]["additionalContext"]
        self.assertIn("learned-mid-session-fix", delta_text)
        self.assertNotIn("learned-baseline", delta_text)

    def test_idf_ranking_prefers_specific_architecture_skill_over_frequent_boilerplate_words(self):
        skills = [
            {"name": "learned-enterprise-document-research", "description": "Use enterprise Codex MCP tools to read internal docs", "version": "1"},
            {"name": "learned-team-workload-monitoring", "description": "Inspect enterprise Codex team workload metrics via MCP", "version": "1"},
            {"name": "learned-delimited-text-ingestion", "description": "Validate delimited text ingestion in Codex pipelines", "version": "1"},
            {"name": "learned-plugin-capability-verification", "description": "Verify Codex plugin and MCP tool capabilities", "version": "1"},
            {"name": "learned-architecture-comparison", "description": "Compare system architecture, skill discovery mechanisms and recall tradeoffs", "version": "1"},
        ]
        pubs = {
            s["name"]: {"catalog_entry": {**s, "artifact_version": "1"}, "version": "1", "audience": "public"}
            for s in skills
        }
        provenance = {
            "version": 1,
            "memory": {},
            "skills": {s["name"]: {"projects": [str(self.project)]} for s in skills},
        }
        ctx = {"checked_publication": True, "skills": skills, "publications": pubs, "bridge_provenance": provenance, "memories": {}}
        view = context_view(ctx, str(self.project), "现在codex+hermes这套框架，skills的发现架构方案如何对比权衡？")
        self.assertEqual(view["recommended_skills"][0], "learned-architecture-comparison")

    def test_native_file_reads_record_delivery_and_leave_test_attribution_to_explicit_feedback(self):
        version, pub = _write_artifact(self.home, "learned-csv-guard", "Always normalize BOM before header split")
        other_version, other_pub = _write_artifact(self.home, "learned-typography", "Choose legible glyphs")
        versions = {"learned-csv-guard": version, "learned-typography": other_version}
        self.store.set("publications-v1", {"learned-csv-guard": pub, "learned-typography": other_pub})
        sync_published_skills(self.settings, self.store)
        self.store.close()

        usage_calls = []
        api = SimpleNamespace(
            version=1,
            open=lambda source: {"handle": source["chat"]},
            observe=lambda *a, **kw: {"revision": 1},
            read=lambda **kw: {"delivery_token": "native-tok-" + kw["name"], "version": versions[kw["name"]]},
            record_usage=lambda tok, ev: usage_calls.append((tok, ev)) or {"recorded": True},
        )
        bridge = Bridge(self.settings, api)
        self.addCleanup(bridge.store.close)

        common = {"cwd": str(self.project), "session_id": "chat-native"}

        def run_turn(turn, command, exit_code):
            def emit(event, **fields):
                hook({**common, "turn_id": turn, "hook_event_name": event, **fields}, self.settings)
            emit("UserPromptSubmit", prompt="Read the references, then run the requested tests")
            for name in versions:
                skill_path = self.codex_skills / name / "SKILL.md"
                emit("PostToolUse", tool_use_id="read-" + name, tool_name="Bash",
                     tool_input={"cmd": f"cat {skill_path}"}, tool_response={"exit_code": 0, "output": skill_path.read_text()})
            emit("PostToolUse", tool_use_id="test", tool_name="Bash",
                 tool_input={"command": command}, tool_response={"exit_code": exit_code})
            emit("Stop", last_assistant_message="Tests finished")
            bridge.ingest()
            return bridge.rpc("feedback_status", {"cwd": str(self.project), "chat": "chat-native", "turn": turn})

        unrelated = run_turn("turn-1", "pytest tests/test_billing.py", 1)
        self.assertEqual({(read["name"], read["version"]) for read in unrelated["deliveries"]}, set(versions.items()))
        self.assertEqual(len(usage_calls), 2)
        self.assertEqual(unrelated["test_references"][0]["outcome"], "failed")
        self.assertEqual(unrelated["applications"], [])
        self.assertEqual(sync_published_skills(self.settings, bridge.store)["projected_skills"], sorted(versions))

        targeted = run_turn("turn-2", "pytest tests/test_csv_headers.py", 1)
        token = next(key.removeprefix("issued-delivery:") for (key,) in bridge.store.db.execute(
            "SELECT key FROM meta WHERE key LIKE 'issued-delivery:%' AND json_extract(value,'$.name')=? "
            "AND json_extract(value,'$.source_event[1]')=?", ("learned-csv-guard", "turn-2"),
        ))
        bound = bridge.rpc("record_application", {"cwd": str(self.project), "delivery_token": token,
                           "stage": "failed", "source_events": [targeted["test_references"][0]["source_event"]]})
        self.assertEqual((bound["version"], bound["validation"]), (version, "host_test_failed"))
        self.assertEqual(sync_published_skills(self.settings, bridge.store)["projected_skills"], ["learned-typography"])

    def test_plain_hook_output_confirms_full_reads_or_partial_reads_with_host_exit(self):
        from functools import partial
        from hermes_codex_learning.host_evidence import read_turn

        name = "learned-csv-guard"
        version, publication = _write_artifact(self.home, name, "Normalize BOM before splitting the header.\n")
        self.store.set("publications-v1", {name: publication})
        sync_published_skills(self.settings, self.store)
        self.store.close()
        usage_calls = []
        api = SimpleNamespace(
            version=1,
            open=lambda source: {"handle": source["chat"]},
            observe=lambda *args, **kwargs: {"revision": 1},
            read=lambda **kwargs: {"delivery_token": "native-fixture", "version": version},
            record_usage=lambda token, evidence: usage_calls.append(evidence["source_event"][2]),
        )
        bridge = Bridge(self.settings, api)
        self.addCleanup(bridge.store.close)

        chat, turn = "chat-native", "terminal-turn"
        transcript = self.root / "rollout.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": chat, "cwd": str(self.project)}}]
        for call, code in (("partial", 0), ("nonzero", 1), ("different-content", 0)):
            rows.append({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
                "id": call, "type": "CommandExecution", "status": "completed" if code == 0 else "failed",
                "exit_code": code,
            }}})
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

        common = {"cwd": str(self.project), "session_id": chat, "turn_id": turn, "transcript_path": str(transcript)}
        skill = self.codex_skills / name / "SKILL.md"
        body = skill.read_text()
        excerpt = "Normalize BOM before splitting the header.\n"
        with patch("hermes_codex_learning.host_evidence.read_turn", partial(read_turn, sessions_root=self.root)):
            hook({**common, "hook_event_name": "UserPromptSubmit", "prompt": "Read the released skill"}, self.settings)
            for call, command, response in (
                ("full", f"cat {skill}", body),
                ("partial", f"tail -1 {skill}", excerpt),
                ("unconfirmed-partial", f"tail -1 {skill}", excerpt),
                ("nonzero", f"cat {skill}", body),
                ("different-content", f"tail -1 {skill}", "A different file\n"),
            ):
                hook({**common, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": call,
                      "tool_input": {"command": command}, "tool_response": response}, self.settings)
            hook({**common, "hook_event_name": "Stop", "last_assistant_message": "Read the skill"}, self.settings)
            bridge.ingest()
            bridge.ingest()

        self.assertCountEqual(usage_calls, ["full", "partial"])
        feedback = bridge.rpc("feedback_status", {"cwd": str(self.project), "chat": chat, "turn": turn})
        self.assertEqual(feedback["deliveries"], [{"name": name, "version": version}] * 2)
        self.assertEqual(feedback["applications"], [])

    def test_scoped_history_search_uses_fts5_and_enforces_workspace_isolation(self):
        import sys
        fake_mod = SimpleNamespace()

        class FakeSessionDB:
            def __init__(self, db_path, read_only=True):
                self.conn = sqlite3.connect(db_path)

            def search_messages(self, query, role_filter=None, exclude_sources=None, limit=50, offset=0):
                rows = self.conn.execute(
                    "SELECT session_id, role, content, timestamp FROM messages_fts WHERE messages_fts MATCH ? LIMIT ?",
                    (query, limit),
                ).fetchall()
                return [{"session_id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                        for r in rows if not role_filter or r[1] in role_filter]

            def get_messages(self, handle, include_ancestors=False):
                rows = self.conn.execute(
                    "SELECT role, content, timestamp FROM messages_fts WHERE session_id=?", (handle,)
                ).fetchall()
                return [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in rows]

            def close(self):
                self.conn.close()

        fake_mod.SessionDB = FakeSessionDB
        db_path = self.home / "state.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(session_id UNINDEXED, role UNINDEXED, content, timestamp UNINDEXED)")
        conn.execute("INSERT INTO messages_fts VALUES ('h-proj-a', 'assistant', 'Resolved SQLite WAL lock contention with busy_timeout and API_KEY=synthetic-secret-value', '2026-11-19T10:00:00Z')")
        conn.execute("INSERT INTO messages_fts VALUES ('h-proj-b', 'assistant', 'Other project WAL secret record', '2026-11-19T10:05:00Z')")
        conn.commit()
        conn.close()

        self.store.set("scope:h-proj-a", {"host": "local", "project": str(self.project), "chat": "c-a", "executor": "Codex App"})
        self.store.set("scope:h-proj-b", {"host": "local", "project": str(self.other), "chat": "c-b", "executor": "Codex App"})
        self.store.close()

        prev = sys.modules.get("hermes_state")
        sys.modules["hermes_state"] = fake_mod
        try:
            bridge = Bridge(self.settings, SimpleNamespace(version=1))
            self.addCleanup(bridge.store.close)
            res_a = bridge.rpc("history_search", {"cwd": str(self.project), "query": "SQLite WAL contention?"})
            self.assertTrue(res_a["success"])
            self.assertEqual(len(res_a["results"]), 1)
            self.assertEqual(res_a["results"][0]["session_id"], "h-proj-a")
            self.assertEqual(res_a["results"][0]["engine"], "native_fts5")
            excerpt = res_a["results"][0]["messages"][0]["content"]
            self.assertIn("busy_timeout", excerpt)
            self.assertNotIn("synthetic-secret-value", excerpt)

            res_b = bridge.rpc("history_search", {"cwd": str(self.other), "query": "busy_timeout"})
            self.assertEqual(res_b["results"], [])
        finally:
            if prev is None:
                sys.modules.pop("hermes_state", None)
            else:
                sys.modules["hermes_state"] = prev

    def test_ephemeral_settings_and_foreign_owner_do_not_clobber_host_skills_dir(self):
        ephemeral_cfg = self.root / "ephemeral-bridge.json"
        atomic_json(ephemeral_cfg, {
            "home": str(self.home),
            "project": str(self.project),
            "source": str(self.root),
            "python": "/usr/bin/python3",
            "capture_scope": "global",
            "checked_publication": True,
        })
        loaded_ephemeral = Settings.load(ephemeral_cfg)
        self.assertIsNone(loaded_ephemeral.codex_skills_dir)

        fake_user_home = self.root / "fake-user-home"
        pilot_home = fake_user_home / ".hermes-codex-learning" / "pilot"
        pilot_home.mkdir(parents=True)
        pilot_cfg = pilot_home / "bridge.json"
        atomic_json(pilot_cfg, {
            "home": str(pilot_home),
            "project": str(self.project),
            "source": str(self.root),
            "python": "/usr/bin/python3",
            "capture_scope": "global",
            "checked_publication": True,
        })
        with patch.dict(os.environ, {"HOME": str(fake_user_home)}):
            loaded_host = Settings.load(pilot_cfg)
        self.assertEqual(loaded_host.codex_skills_dir, (fake_user_home / ".agents" / "skills").absolute())

        _, pub_primary = _write_artifact(self.home, "learned-primary", "Primary owner skill")
        self.store.set("publications-v1", {"learned-primary": pub_primary})
        primary_sync = sync_published_skills(self.settings, self.store)
        self.assertEqual(primary_sync["projected_skills"], ["learned-primary"])
        before_manifest = (self.codex_skills / MANIFEST_NAME).read_text(encoding="utf-8")

        secondary_home = self.root / "secondary-pilot"
        secondary_home.mkdir()
        secondary_settings = Settings(
            secondary_home, self.project, self.root, Path("/usr/bin/python3"),
            capture_scope="global", checked_publication=True,
            native_skill_projection=True, codex_skills_dir=self.codex_skills,
        )
        secondary_store = Store(secondary_settings.state)
        self.addCleanup(secondary_store.close)
        _, pub_secondary = _write_artifact(secondary_home, "learned-intruder", "Secondary fixture skill")
        secondary_store.set("publications-v1", {"learned-intruder": pub_secondary})
        secondary_sync = sync_published_skills(secondary_settings, secondary_store)
        self.assertEqual(secondary_sync["projected_skills"], [])
        self.assertEqual(secondary_sync["shadowed"], {"learned-intruder": "owned_by_other_learning_home"})
        self.assertFalse((self.codex_skills / "learned-intruder").exists())
        self.assertEqual((self.codex_skills / MANIFEST_NAME).read_text(encoding="utf-8"), before_manifest)

    def test_configure_hermes_external_dir_backs_up_and_refuses_owner_home(self):
        import sys
        fake_yaml = SimpleNamespace(
            safe_load=lambda text: json.loads(text) if text.strip() else {},
        )
        fake_cli_cfg = SimpleNamespace(
            atomic_config_write=lambda path, data: path.write_text(json.dumps(data), encoding="utf-8"),
        )
        profile = self.root / "hermes-user-profile"
        profile.mkdir()
        (profile / "config.yaml").write_text(json.dumps({"_config_version": 1, "skills": {"external_dirs": []}}), encoding="utf-8")
        (self.home / "config.yaml").write_text(json.dumps({"_config_version": 1}), encoding="utf-8")

        with self.assertRaises(ValueError):
            configure_hermes_external_dir(self.settings, self.home, apply=True)

        saved = {k: sys.modules.get(k) for k in ("hermes_yaml", "hermes_cli", "hermes_cli.config")}
        sys.modules["hermes_yaml"] = fake_yaml
        sys.modules["hermes_cli"] = SimpleNamespace(config=fake_cli_cfg)
        sys.modules["hermes_cli.config"] = fake_cli_cfg
        try:
            preview = configure_hermes_external_dir(self.settings, profile, apply=False)
            self.assertFalse(preview["applied"])
            applied = configure_hermes_external_dir(self.settings, profile, apply=True)
            self.assertTrue(applied["applied"])
            self.assertTrue(Path(applied["backup"]).is_file())
            written = json.loads((profile / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(written["skills"]["external_dirs"], [str(catalog_root(self.settings).resolve())])
            idempotent = configure_hermes_external_dir(self.settings, profile, apply=True)
            self.assertTrue(idempotent["already_configured"])
            self.assertFalse(idempotent["applied"])
        finally:
            for k, mod in saved.items():
                if mod is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = mod


if __name__ == "__main__":
    unittest.main()
