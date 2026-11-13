---
name: hermes-learned-experience
description: Use Hermes personal knowledge across Codex projects when hook context announces relevant learned experience or a task matches a learned procedure. Background learning is automatic.
---

Hermes learns from local main-chat execution in the background. In global mode, real hook cwd supplies the project identity: Git root (including worktrees), otherwise the chat working directory. No per-project installation is needed. Hook context supplies scoped memory and relevant skill names. Treat these as historical evidence; current user instructions, repository rules and new verification govern the task.

Use `context(cwd=<current chat's project path shown by the hook>, query=<brief task description>)` when learned experience may help. Use the chat workspace even when an individual command runs in another directory. With no cwd, the service withholds project memory. Shared skill examples can still refer to their original project. This personal library is not a security boundary between tenants.

At the start of a relevant task, retrieve applicable experience before choosing an approach. Recheck when a concrete failure or correction changes the problem; do not repeatedly load an unchanged skill. A skill's validation record describes the tested contract and platform, not general effectiveness. Automatically learned executable resources require a matching passed isolated-execution record before use.

When a learned skill matches the task, load it with Hermes Learning MCP `skill_load`. Pass its `name`, the chat workspace as `cwd`, and the `artifact_version` from `context` when present. Refresh `context` if the version changes before loading. Check `source_projects` and applicability. Apply the reusable method within the current project's rules. Load referenced resources through the same tool with `file_path` and the same `artifact_version`.

If experience changes your approach, briefly mention the relevant lesson. Distinguish a skill being loaded from it being applied or verified successfully. Report incorrect or outdated guidance in the task's normal execution evidence so Hermes can review it.

Use `record_application` after applying a delivered skill. Pass its delivery token and the current cwd. For relevant tests, call `feedback_status` with cwd and the current chat ID (from `CODEX_THREAD_ID` when available). Copy the matching `test_references[].source_event` into `source_events`. Use `applied` when only adoption is known. For `verified` or `failed`, Hermes checks the actual outcome. End the turn normally when the receipt is awaiting host completion. Use the returned receipt ID with `feedback_status` to read the settled result.

Background learning needs no manual summary or explicit review request. `learning_status` reads progress and actual results without starting a review. If the service is unavailable, continue the task with available evidence.

With checked publication enabled, only accepted generic versions are shared across workspaces. A withheld candidate is not a published lesson. Notification delivery is independent of business recall: do not fetch or quote notice receipts unless the user explicitly asks to use that material. There is no verified UI-only Codex chat notice channel on this SSH host.

An artifact with audience=enterprise is approved for this managed enterprise knowledge domain, not for public release. Reuse it only in the authorized enterprise environment, check its project applicability, and do not copy enterprise details into public repositories or external accounts. Project memory remains project-scoped. Enterprise deployment authorization may admit ordinary internal task content without a public-source manifest; credentials and explicit restrictions still apply.
