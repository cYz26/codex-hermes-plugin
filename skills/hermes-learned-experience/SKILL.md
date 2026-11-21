---
name: hermes-learned-experience
description: Use Hermes personal knowledge across Codex projects when hook context announces relevant learned experience or a task matches a learned procedure. Background learning is automatic.
---

Hermes learns from local main-chat execution in the background. In global mode, real hook cwd supplies the project identity: Git root (including worktrees), otherwise the chat working directory. No per-project installation is needed. Hook context supplies scoped memory and relevant skill names. Treat these as historical evidence; current user instructions, repository rules and new verification govern the task.

Published generic and enterprise skills are materialized as immutable read-only snapshots and projected into Codex's native skills directory (`~/.agents/skills/`). Prefer Codex's native skill catalog and read projected `SKILL.md` files and bundled resources directly. The bridge records confirmed reads against the delivered version.

Scoped project memory is injected once at `SessionStart` via hook context (and updated only when memory changes or a new skill not yet in the session's initial scan becomes relevant). Use `context(cwd=<current chat's project path>, query=<brief task description>)` and `skill_load` mainly when the hook announces an unprojected project-private (`worktree`) skill, a skill newly learned mid-session, or when you need to inspect unpublished project experience. With no cwd, the service withholds project memory.

When loading an unprojected or mid-session skill via Hermes Learning MCP `skill_load`, pass its `name`, the chat workspace as `cwd`, and the `artifact_version` from `context` when present. Refresh `context` if the version changes before loading. Check `source_projects` and applicability, and load referenced resources through `skill_load` with `file_path` and the same `artifact_version`. When investigating recurring workspace failures or past cases, call `history_search(cwd=<current chat workspace>, query=<concise terms>)` to query Hermes native SessionDB FTS5 history within the current project scope.

If experience changes your approach, briefly mention the relevant lesson. Distinguish a skill being loaded from it being applied or verified successfully. Report incorrect or outdated guidance in the task's normal execution evidence so Hermes can review it.

To verify a skill, pass the delivery token from `skill_load` and references for tests that exercise the delivered skill to `record_application`. Obtain the host's test references from `feedback_status`. Use `stage=applied` to record adoption when you have no skill-specific test.

Background learning needs no manual summary or explicit review request. `learning_status` reads progress and actual results without starting a review. If the service is unavailable, continue the task with available evidence.

With checked publication enabled, only accepted generic versions are shared across workspaces. A withheld candidate is not a published lesson. Notification delivery is independent of business recall: do not fetch or quote notice receipts unless the user explicitly asks to use that material. There is no verified UI-only Codex chat notice channel on this SSH host.

An artifact with audience=enterprise is approved for this managed enterprise knowledge domain, not for public release. Reuse it only in the authorized enterprise environment, check its project applicability, and do not copy enterprise details into public repositories or external accounts. Project memory remains project-scoped. Enterprise deployment authorization may admit ordinary internal task content without a public-source manifest; credentials and explicit restrictions still apply.
