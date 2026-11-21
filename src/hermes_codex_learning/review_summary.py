"""A bounded, redacted explanation for review notifications, separate from execution facts."""
from __future__ import annotations

import re
from contextlib import contextmanager

from .review_input import is_pending_tool_reply
from .enterprise import scrub

MAX_SUMMARY_CHARS = 1200

SUMMARY_PROMPT = (
    "\n\nFinish this same review with a concise explanation in 2–3 sentences, in Simplified Chinese: "
    "the problem or correction that prompted the review, the reusable conclusion with its supporting "
    "evidence, and its applicability or limitations. Summarize only the supplied evidence; if it "
    "contains only workflow metadata, say that task-specific evidence is unavailable. Do not repeat "
    "the full memory/skill changes, include private reasoning, or call tools just to prepare this "
    "explanation. Publication and subsequent task validation are reported separately from execution "
    "receipts; do not claim that they have occurred."
)

_PROJECT_MEMORY_PROMPT = (
    "\n\nFor a user-declared standing convention or a durable fact specific to the source workspace, "
    "use the memory tool with target='memory'. That target writes the current project's MEMORY.md. "
    "Evaluate project memory independently of skill changes. Add a concise, grounded entry for a new "
    "fact when warranted. USER.md is reserved for user preferences that apply across projects. "
    "Leave one-off instructions and transient task details out of both stores."
)
_PROJECT_SKILL_PROMPT = (
    "\n\nA skill_view result marked read_only is reference material. Respect workspace ownership "
    "and stop retrying an ownership refusal. For a distinct, reusable procedure that belongs only "
    "to the current project, check for an existing owned skill or create a distinct project skill "
    "without a global_candidate designation."
)


def summary_text(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # Redact before clipping so a cut credential cannot escape the existing filter.
    text = scrub(value).strip()
    # Chinese prose can touch a key without a Unicode word boundary.
    text = re.sub(r"\b(?:sk-[a-zA-Z0-9_-]{12,}|gh[pousr]_[a-zA-Z0-9_]{16,})",
                  "[REDACTED]", text, flags=re.ASCII)
    if is_pending_tool_reply(text) or text.lstrip("⚠️ ").startswith("No reply:"):
        return None
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def _final_summary(result) -> str | None:
    if not isinstance(result, dict) or any(result.get(key) for key in ("failed", "interrupted", "error")):
        return None
    return summary_text(result.get("final_response"))


class _ReviewSummary:
    def __init__(self):
        self.handle = None
        self.current = None

    @contextmanager
    def capture(self, handle):
        previous = self.handle, self.current
        self.handle, self.current = handle, {"text": None}
        try:
            yield self.current
        finally:
            self.handle, self.current = previous


@contextmanager
def capture_review_summary(api, handle):
    """Keep the explanation in the notification path, outside native receipts and MCP state."""
    runtime = getattr(getattr(api, "_service", None), "runtime", None)
    collector = getattr(runtime, "_notice_review_summary", None)
    if not isinstance(collector, _ReviewSummary):
        yield {"text": None}
        return
    with collector.capture(handle) as captured:
        yield captured


def install_summary_capture(runtime, settings=None) -> None:
    """Read the pinned native fork's final response during this serial worker's own review."""
    from contextlib import nullcontext

    from agent import background_review

    from .scheduling import REVIEW_REASONS
    from .skill_growth import GUIDANCE, constrain_writes
    from .workflow import EXECUTOR

    if isinstance(getattr(runtime, "_notice_review_summary", None), _ReviewSummary):
        return
    collector = runtime._notice_review_summary = _ReviewSummary()
    native_review = runtime.review
    scoped_storage = settings is not None and settings.uses_scoped_storage

    def review(handle, source, messages, intent):
        collect = collector.handle == handle
        automatic_skills = (intent.get("reason") in REVIEW_REASONS
                            and (intent.get("skills") or source.get("executor") == EXECUTOR))
        if not collect and not automatic_skills and not scoped_storage:
            return native_review(handle, source, messages, intent)
        native_fork = background_review._run_review_fork

        def fork(parent, history, prompt, task_cfg, review_run, st, review_memory=False, explicit=False):
            own_review = parent.session_id == handle
            if own_review and automatic_skills:
                prompt += GUIDANCE
            if own_review and scoped_storage:
                from agent.shared_learning_scope import current
                if (scope := current.get()) is not None and scope.project:
                    if review_memory and parent._memory_enabled:
                        prompt += _PROJECT_MEMORY_PROMPT
                    if intent.get("skills"):
                        prompt += _PROJECT_SKILL_PROMPT
            if own_review and collect:
                prompt += SUMMARY_PROMPT
            try:
                with constrain_writes() if own_review and automatic_skills else nullcontext():
                    return native_fork(parent, history, prompt, task_cfg, review_run, st, review_memory, explicit)
            finally:
                if own_review and collect:
                    collector.current["text"] = _final_summary(st.result)

        # The existing worker serializes reviews and Curator. Restore the native
        # entry even when the review fails; other sessions keep their original prompt.
        background_review._run_review_fork = fork
        try:
            return native_review(handle, source, messages, intent)
        finally:
            background_review._run_review_fork = native_fork

    runtime.review = review
