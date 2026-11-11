"""Only this perimeter plugin knows the Codex-specific bridge package."""
from contextlib import ExitStack


def register(ctx):
    ctx.register_system_prompt_section(
        "codex-learning-sharing",
        "Project-specific experience must retain its original applicability. When a skill is genuinely "
        "generalizable, create an abstracted separate skill with learning_scope: global_candidate in "
        "its YAML frontmatter. Remove project paths, private identifiers, internal links and credentials. "
        "State prerequisites, limitations and verification steps. The bridge independently checks "
        "candidates before sharing; do not copy private evidence into generic examples.",
        max_chars=1000,
    )

    def setup(parser):
        parser.add_argument("--settings", required=True)

    def serve(args):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.daemon import run
        from hermes_codex_learning.review_input import install_review_policy
        from hermes_codex_learning.review_receipt import ReviewWrites

        review_writes = ReviewWrites()
        with ExitStack() as registrations:
            for hook, callback in (("pre_tool_call", review_writes.record_attempt),
                                   ("post_tool_call", review_writes.record)):
                registrations.callback(ctx.register_hook(hook, callback).dispose)
            settings = Settings.load(args.settings)
            api = ctx.learning(version=1)
            workflow_registration = None
            semantic_registration = None
            try:
                from hermes_codex_learning.workflow_review import install_workflow_policy
                workflow_registration = install_workflow_policy(api, settings, ctx)
                from hermes_codex_learning.semantic_review import install_semantic_policy
                semantic_registration = install_semantic_policy(api, settings, ctx)
                install_review_policy(api, settings)
            except Exception:
                api.close()
                raise
            try:
                return run(settings, api, review_writes)
            finally:
                if semantic_registration is not None:
                    semantic_registration.dispose()
                if workflow_registration is not None:
                    workflow_registration.dispose()

    ctx.register_cli_command("learning-bridge", "Run the project-scoped durable learning bridge",
                             setup_fn=setup, handler_fn=serve)
