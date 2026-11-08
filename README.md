# Hermes Learning for Codex

新装和升级见 [安装与更新](./docs/INSTALL.zh-CN.md)。[文档索引](./docs/README.md) 区分当前实现、验收记录和历史研究；[证据索引](./evidence/README.md) 说明测试、真实模型与 App 验收的边界。

Codex executes business tasks. A globally installed plugin records real hooks with per-project source identities; an independent service delegates unattended learning to native Hermes. Model selection remains **gpt-6.1-sol / openai-codex / codex_responses / Max**.

The deployed learning core is **official Hermes v0.23.0**, commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, plus the generic Learning API v1 patch. The bridge and Codex plugin are **0.3.2**. The worker shares the official App installation's committed Python dependencies and loads the patch from a separate pinned source view. See the [upgrade workflow](./docs/UPDATES.zh-CN.md) and [v0.23.0 compatibility checks](./docs/UPGRADE-v0.23.0.zh-CN.md). Existing Hermes App authentication is reused through native profile fallback; memory, skills, sessions and bridge data remain independent. The [0.2.2 acceptance report](./docs/RELEASE-0.2.2.zh-CN.md) and [historical acceptance](./ACCEPTANCE.json) record v0.22.7.

## Current deployment

- Core source: a verified v0.23.0 patch view under the private `updates/` directory, prepared from the local official installation. Read the exact paths with `scripts/bridge update status`.
- Private data: `/Users/cy/.hermes-codex-learning/pilot`.
- Hidden native home entry: `/Users/cy/.hermes/profiles/.codex-learning-pilot`, a directory link to the private data.
- Existing auth root: `/Users/cy/.hermes`; no copied token file and no profile-local `auth.json`.
- Interpreters: the supervisor uses the local Python that registered its LaunchAgent. `bridge.json` selects the worker's published official environment and a verified private fallback; preserve the venv paths as written.
- Service: `com.cy.hermes-codex-learning.pilot`.
- Global capture configuration: all local main chats with a real hook cwd; Git roots/worktrees or non-Git working directories retain separate source identities. No old transcript is imported. Deployment and host-trust gates are tracked in [global rollout](./docs/GLOBAL-ROLLOUT.zh-CN.md).

The native profile has `gateway.standalone: true` so the App's host gateway does not serve it. The App's source, package and running process were not replaced or restarted. Native root credentials use the same locking and refresh write-back path; native profile config and learned artifacts stay local to the separate home.

**This is a hidden background learning scope.** Its former visible `codex-learning-pilot` registration caused `Runtime not ready` / `profile_mismatch` when selected in Desktop. The registration now uses `.codex-learning-pilot`: native enumeration skips it, so both the App profile API and Bots RPC omit it. Authentication fallback and the physical learning home are unchanged. Use `scripts/bridge` to inspect it; it is no longer a selectable `hermes -p` name. See [Desktop profile boundary and diagnosis](./docs/PROFILE-DESKTOP.zh-CN.md).

## Inspect and use

**0.3.2 adds the [Learning Loop icon](./docs/design/hermes-learning-icon/README.md) and bounds background review input.** Large attachments and tool output are compacted in the review copy; the recorded task evidence is kept. The supervisor can start when a Hermes worker environment is missing. Official updates still require verification against the exact target; see [更新策略、兼容性与回退](./docs/UPDATES.zh-CN.md).

```sh
scripts/bridge update status
scripts/bridge update check
```

在本工程的 Codex 聊天发 **“自动验收 Hermes 跨项目召回”**，由 Codex 创建两条真实 App 聊天并核对各自的召回来源。[自动验收流程](./docs/GLOBAL-ROLLOUT.zh-CN.md#按需自动验收)

```sh
scripts/bridge auth-status
scripts/bridge status
```

`auth-status` exports only a native read-only status, not credentials. `login` returns the existing status when Hermes is already logged in; no additional authorization is needed for this deployment. Account recovery belongs to the existing Hermes login flow. The bridge never imports Codex App authentication.

The plugin selector is `hermes-learning@hermes-learning-local`. See [installation verification](./docs/INSTALL.zh-CN.md#安装后回读) for hook trust checks. Each hook response limits its full `additionalContext` to 256 UTF-8 bytes, a conservative upper bound for the host's byte-level BPE. Codex can retrieve the complete project context or load a relevant skill through MCP when the preview is clipped.

Learning feedback appears in macOS notifications. Each banner shows the project and counts, and opens the review's local receipt. The receipt shows when the review started and ended, applied memory and skill changes, and pending or incomplete outcomes. It keeps the operation details in the pilot's private data directory. Older records can show the original operation list when the change text is unavailable. Codex hooks continue to capture activity and supply project-scoped recall to the model. Set `notifications` to `false` in `bridge.json` to disable the macOS channel on the next bridge start.

## Verification and recovery

```sh
python3 -B scripts/check_release.py
```

This portable check runs the bridge tests and verifies package versions, patch integrity, preserved evidence, local document links and the absence of credential material in the publishable tree. It does not call a model, install a plugin or modify a live Hermes profile. The extra packaging regression executes the MCP manifest exactly, without hook environment variables or placeholder expansion.

Run native tests through `scripts/run_tests.sh` from the Hermes checkout with `HERMES_PYTHON` set to the test interpreter. The preserved v0.22.7 core baseline covers 328 original and 343 modified native tests and all 14 native checks. The [v0.23.0 shared candidate](./evidence/updates/v0.3.1.json) passed 333 native tests before the 0.3.2 bridge changes.

`probes/check_hidden_profile.py --source /absolute/hermes-source`, run with the dedicated Hermes interpreter, separately checks hidden enumeration, shared OAuth refresh write-back and independent Learning API data using temporary dummy credentials. The [visibility follow-up evidence](./evidence/v0.22.7/hidden-profile.json) records live App REST / Bots RPC list checks; it does not claim direct UI or new model acceptance.

`probes/real_learning.py` is an opt-in real-provider contract probe through the public plugin facade. Stop the pilot owner first. It labels its evidence as a synthetic fixture, uses native authentication and saves outcomes/usage separately. It may retain real pilot conventions through native memory/skill rules. It cannot establish App hooks or automatic triggering; do not count it as a captured App conversation.

Admission/counters/intents are durable. Stable message UIDs support transcript projection; a lost in-progress projection or review becomes uncertain/unknown and is not silently replayed. Native files, state.db, the learning journal and remote providers are not one cross-storage transaction. Usage receipts remain best-effort telemetry. Notification dispatch does not prove OS visibility; skill delivery does not prove adoption or successful application.

On upgrade, stop only this pilot, snapshot its data and service config, reapply the generic patch on a new official stable candidate, and run native and plugin-contract regression before switching. Keep root authentication managed by Hermes; do not restore a stale copy of rotating credentials. Rollback details and snapshot pointer are in [IMPLEMENTATION.md](./IMPLEMENTATION.md).

## Repository layout

```text
src/hermes_codex_learning/   Durable bridge, hooks, IPC, MCP, auth diagnostics
hermes-plugin/              Native Hermes plugin entry point
hooks/ skills/              Codex hook declarations and learned-skill loading policy
assets/                     Codex plugin icon
patches/                    Hermes Learning API patch and pinned manifest
scripts/                    Client/service entry points and release checks
probes/                     Explicit real-provider verification fixture
tests/                      Portable bridge regression
docs/                       Overall design, documentation index and archives
evidence/                   Versioned verification records and public summaries
third_party/                Upstream license notice
```

Hermes itself is obtained from the pinned official source and patched; the full upstream checkout, runtime, user authentication, learned data and rollback snapshots are not vendored. `source_committed` in the patch manifest describes the separate Hermes checkout, not whether this plugin repository is committed. See [third-party notices](./THIRD_PARTY_NOTICES.md).
