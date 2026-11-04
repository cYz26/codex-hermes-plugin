# Hermes Learning for Codex

完整中文方案见 [Codex 执行、Hermes 学习：整体方案](./docs/ARCHITECTURE.zh-CN.md)。[文档索引](./docs/README.md) 区分当前实现、验收记录和历史研究；[证据索引](./evidence/README.md) 说明测试、真实模型与 App 验收的边界。

Codex executes business tasks. A project-scoped plugin records real hooks; an independent service delegates unattended learning to native Hermes. Model selection remains **gpt-6.1-sol / openai-codex / codex_responses / Max**.

The deployed core is **official Hermes v0.22.7**, commit `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`, plus the generic Learning API v1 patch. The bridge and Codex plugin are **0.1.3**. Existing Hermes App authentication is reused through native profile fallback; memory, skills, sessions and bridge data remain independent. See [implementation and evidence](./IMPLEMENTATION.md) and [machine-readable acceptance](./ACCEPTANCE.json).

## Current deployment

- Core source: `/Users/cy/Dev/agents-dev/hermes-learning-core`, branch `feat/learning-api-v1-v0.22.7`.
- Private data: `/Users/cy/.hermes-codex-learning/pilot`.
- Native profile entry: `/Users/cy/.hermes/profiles/codex-learning-pilot`, a directory link to the private data.
- Existing auth root: `/Users/cy/.hermes`; no copied token file and no profile-local `auth.json`.
- Interpreter: `/Users/cy/.hermes-codex-learning/runtime-v0.22.7/bin/python` (preserve the venv spelling).
- Service: `com.cy.hermes-codex-learning.pilot`.
- Only collected project: `/Users/cy/Dev/agents-dev/hermes-plugin1102/pilot-project`, main chats only.

The native profile has `gateway.standalone: true` so the App's host gateway does not serve it. The App's source, package and running process were not replaced or restarted. Native root credentials use the same locking and refresh write-back path; native profile config and learned artifacts stay local to the separate home.

## Inspect and use

```sh
scripts/bridge auth-status
scripts/bridge status
```

`auth-status` exports only a native read-only status, not credentials. `login` returns the existing status when Hermes is already logged in; no additional authorization is needed for this deployment. Account recovery belongs to the existing Hermes login flow. The bridge never imports Codex App authentication.

The installed plugin is `hermes-learning@hermes-learning-local`, version 0.1.3. Review/trust its hooks in the normal Codex UI when required; installation does not prove trust or loading. Use the pilot project for real App acceptance. The native real-provider probe and installed stdio MCP test are separate from that App evidence.

## Install another isolated instance

Start from the exact official commit, apply [the patch](./patches/native-learning-api-v1.patch), and verify [its manifest](./patches/manifest.json). From that checkout, use native PM:

```sh
HERMES_HOME=/absolute/learning/pilot HERMES_RUNTIME_DIR=/absolute/learning/tools \
python3 -B -m pm.build_env --source . --out /absolute/learning/runtime --group dev --group test
```

From this plugin directory, initialize a new home:

```sh
/absolute/learning/runtime/bin/python scripts/bridge init \
  --source /absolute/hermes-source --python /absolute/learning/runtime/bin/python \
  --project /absolute/pilot-project --home /absolute/learning/pilot \
  --profile-home /absolute/existing-hermes-root/profiles/codex-learning-pilot
```

The CLI refuses an existing config or a profile entry belonging to a different data home. It writes a fresh native config with Max, unattended write policy, native review enabled and standalone gateway mode. It does not clone another profile's config or artifacts. The profile entry allows native authentication fallback from the existing root. External CLI login adoption stays disabled.

`bridge.json` records the physical `home`, lexical `profile_home`, source and interpreter. Clients may select another settings file with `HERMES_LEARNING_CONFIG`. All native launch paths derive the same two native environment variables; resolving `profile_home` too early would disable authentication inheritance.

```sh
scripts/bridge launchd --settings /absolute/learning/pilot/bridge.json --start
```

## Verification and recovery

```sh
python3 -B scripts/check_release.py
```

This portable check runs the 9 bridge tests and verifies package versions, patch integrity, preserved evidence, local document links and the absence of credential material in the publishable tree. It does not call a model, install a plugin or modify a live Hermes profile.

Run native tests through `scripts/run_tests.sh` from the Hermes checkout with `HERMES_PYTHON` set to the dedicated interpreter. The final v0.22.7 evidence covers 328 original and 343 modified native tests, 9 bridge tests and all 14 native checks. Authentication tests include source-aware OAuth refresh and data/gateway isolation.

`probes/real_learning.py` is an opt-in real-provider contract probe through the public plugin facade. Stop the pilot owner first. It labels its evidence as a synthetic fixture, uses native authentication and saves outcomes/usage separately. It may retain real pilot conventions through native memory/skill rules. It cannot establish App hooks or automatic triggering; do not count it as a captured App conversation.

Admission/counters/intents are durable. Stable message UIDs support transcript projection; a lost in-progress projection or review becomes uncertain/unknown and is not silently replayed. Native files, state.db, the learning journal and remote providers are not one cross-storage transaction. Usage receipts remain best-effort telemetry. Notification dispatch does not prove OS visibility; skill delivery does not prove adoption or successful application.

On upgrade, stop only this pilot, snapshot its data and service config, reapply the generic patch on a new official stable candidate, and run native and plugin-contract regression before switching. Keep root authentication managed by Hermes; do not restore a stale copy of rotating credentials. Rollback details and snapshot pointer are in [IMPLEMENTATION.md](./IMPLEMENTATION.md).

## Repository layout

```text
src/hermes_codex_learning/   Durable bridge, hooks, IPC, MCP, auth diagnostics
hermes-plugin/              Native Hermes plugin entry point
hooks/ skills/              Codex hook declarations and learned-skill loading policy
patches/                    Hermes Learning API patch and pinned manifest
scripts/                    Client/service entry points and release checks
probes/                     Explicit real-provider verification fixture
tests/                      Portable bridge regression
docs/                       Overall design, documentation index and archives
evidence/                   Versioned verification records and public summaries
third_party/                Upstream license notice
```

Hermes itself is obtained from the pinned official source and patched; the full upstream checkout, runtime, user authentication, learned data and rollback snapshots are not vendored. `source_committed` in the patch manifest describes the separate Hermes checkout, not whether this plugin repository is committed. See [third-party notices](./THIRD_PARTY_NOTICES.md).
