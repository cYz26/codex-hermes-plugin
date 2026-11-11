---
name: hermes-refine
description: Explicitly review a Codex conversation with Hermes and save lessons to project memory or skills when the user asks to learn or refine now.
---

Use this workflow when the user asks Hermes to learn from an executed task or
refine this conversation. Ordinary work continues to use automatic learning.
For a document, directory, URL or supplied notes, use the sibling `hermes-learn` skill.

1. Resolve the actual chat workspace and host chat ID. In the Codex executor,
   `CODEX_THREAD_ID` supplies the chat ID; read that single variable if necessary.
   Do not select the most recent chat in a directory. If the host supplies no ID,
   obtain the intended existing chat ID before submitting.
2. Call `hermes_learning.request_learning` with `cwd`, `chat_id`, a fresh stable
   `request_id`, `mode="refine"`, and the user's `focus`. Choose `scope` from
   `both`, `memory`, `skills`. A repeated transport call uses the same request ID;
   another user-requested learning pass uses a new one, even on the same evidence.
3. `memory_writes="add"` is the default. Use `revise` only when the user asks to
   correct or update existing project memory. It preserves the configured write
   policy and does not authorize changes outside this project's memory.
4. The default snapshot includes only already observed terminal turns at submission.
   An explicit `through_turn` must be a real captured turn from this chat. If it is
   unfinished, the request returns `waiting_for_turn`; finish the current task and
   report the request ID rather than polling for the current turn to end itself.
5. Query `hermes_learning.learning_request_status` with the same cwd/request ID.
   Report queued, running, publishing, completed, failed or unknown accurately.
   Explain a truncated evidence snapshot or missing coverage. Report actual
   applied/staged changes and artifact versions; a completed review with no writes
   is not a new learned skill, and content checks do not prove business effectiveness.

When a user asks to stop this learning request, `cancel_learning` stops queued
work and future batches. An in-flight batch may finish; existing writes are retained.
Do not retry an unknown native attempt under a new ID to manufacture completion.

If new MCP tools are unavailable in an already-running chat, the same installed
plugin exposes `scripts/bridge learning request|status|cancel`; read its `--help`.
Resolve `scripts/bridge` relative to this skill's plugin root and pass the actual
chat workspace through `--cwd`, not the plugin directory. The CLI's `--chat`
defaults to `CODEX_THREAD_ID`. Report `runtime_upgrade_required` as a deployment
gap rather than changing the runtime or configuration as part of ordinary use.
