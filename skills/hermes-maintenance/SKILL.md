---
name: hermes-maintenance
description: 检查、验证和更新本插件专用的 Hermes 学习运行时，评估新版自进化机制变化并协商需要重设计的升级方案。
---

Use `hermes_learning.update_status` and `update_check` to inspect the current runtime,
candidate gates and upstream changes. The managed supervisor checks daily by default,
verifies reviewed candidates in isolation, and automatically switches only after all gates pass.
When an official source is bound, the learning worker borrows the App's committed Python
dependency generation and loads a pinned, private source view with the Learning API patch.
The supervisor follows App upgrades after validation. With `upgrade_official` explicitly
enabled, it can also prepare a candidate first and use the native updater to advance the
official installation to exactly the verified commit.

Read [the upgrade workflow](../../docs/UPDATES.zh-CN.md) when preparing a candidate,
investigating a failed gate, changing policy or adding a version adapter. The CLI at
this plugin's `scripts/bridge update` supports status, check, prepare, apply, rollback, policy and run.
Preserve settings selected by `HERMES_LEARNING_CONFIG` or explicit `--settings`.

For `plan_required`, explain the changes in review triggers, review execution, memory/skill
writes, Curator, API or stored data, whichever the evidence supports. Present the affected
current flow, proposed replacement, migration/rollback approach and verification boundaries.
Agree on the modification and upgrade plan with the user **before** redesigning the workflow
or activating that release. The user's automatic-update policy does not authorize this redesign.
Attribute this pause to the user's maintenance policy, not to a generic approval requirement.

`mechanism_review_required` is uncertainty, not proof of a breaking redesign. Inspect the
official source and native tests first. A truncated comparison or release notes alone cannot
establish compatibility. Small compatible changes may use the existing automatic route;
if redesign is necessary, follow the plan-agreement rule above. Never mark a recipe compatible
just to bypass this gate. An adapted design records the actual agreed plan in `mechanism_review.agreed_plan`.

Report native/bridge fixture tests, actual provider runs, and actual Codex App acceptance
separately. A healthy startup does not prove a new real-provider learning loop or improved
learning quality. Keep historical evidence intact and preserve current credentials and hooks
captured during updates when recovering a failed activation.
