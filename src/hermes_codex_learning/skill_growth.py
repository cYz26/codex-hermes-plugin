"""Apply Hermes' authoring budgets to writes from automatic Codex reviews."""
from contextlib import contextmanager
from contextvars import ContextVar


GUIDANCE = """

For this automatic review, save a change only when the evidence supports a durable,
reusable lesson or a clear user correction that applies to future work. Routine
completion, ordinary followups and already documented practices are insufficient.
Check the current skill and the relevant references before deciding. If the library
already covers the lesson or nothing durable emerged, finish without a write.
Replace redundant rules in place. Keep SKILL.md focused on triggers and steps used
on most tasks. Put optional detail in existing topical references or add a linked
reference when the topic needs one. Create a skill only for an uncovered class of work.
If a native size check refuses a write, compact the existing material or use a topical
reference. Preserve any existing behavior, prerequisites and working reference links.
"""

_ACTIVE = ContextVar("automatic_skill_growth", default=False)


def native_authoring_limits():
    try:
        from tools import skill_linter, skill_manager_tool
        if callable(getattr(skill_manager_tool, "_guarded_write", None)):
            return {
                "body_chars": skill_linter._BODY_SOFT_BUDGET_CHARS,
                "markdown_references": skill_linter._MAX_REFERENCE_FILES,
            }
    except (ImportError, AttributeError):
        pass
    return None


@contextmanager
def constrain_writes():
    limits = native_authoring_limits()
    if limits is None:
        yield  # Older Hermes versions still receive the authoring guidance.
        return

    from tools import skill_linter, skill_manager_tool

    native_write = skill_manager_tool._guarded_write

    def guarded_write(name, skill_dir, target, action, label, content):
        if _ACTIVE.get():
            if target == skill_dir / "SKILL.md":
                _, body = skill_linter.parse_frontmatter(content)
                if len(body) > limits["body_chars"]:
                    previous = target.read_text(encoding="utf-8-sig") if target.exists() else ""
                    _, previous_body = skill_linter.parse_frontmatter(previous)
                    if len(body) > len(previous_body):
                        return {"success": False, "error": (
                            f"{name}/SKILL.md would grow to {len(body):,} body characters "
                            f"above Hermes' {limits['body_chars']:,}-character authoring budget. "
                            "Compact the main instructions or link to a topical reference."
                        )}
            elif counts_as_reference(target, skill_dir) and not target.exists():
                references = sum(counts_as_reference(path, skill_dir)
                                 for path in (skill_dir / "references").rglob("*.md"))
                if references >= limits["markdown_references"]:
                    return {"success": False, "error": (
                        f"{name} already has {references} Markdown references. "
                        "Consolidate or update an existing topical reference."
                    )}
        return native_write(name, skill_dir, target, action, label, content)

    # Native has already resolved the exact replacement at this point. It still
    # owns locking, approvals, read-before-write, batch rollback and persistence.
    skill_manager_tool._guarded_write = guarded_write
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        skill_manager_tool._guarded_write = native_write


def counts_as_reference(path, skill_dir):
    root = skill_dir / "references"
    return (path.suffix == ".md" and path.is_relative_to(root)
            and not any(part.startswith("_") for part in path.relative_to(root).parts))
