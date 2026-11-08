"""Only this perimeter plugin knows the Codex-specific bridge package."""


def register(ctx):
    def setup(parser):
        parser.add_argument("--settings", required=True)

    def serve(args):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.daemon import run
        from hermes_codex_learning.review_input import install_review_policy
        from hermes_codex_learning.review_receipt import ReviewWrites

        review_writes = ReviewWrites()
        registration = ctx.register_hook("post_tool_call", review_writes.record)
        try:
            settings = Settings.load(args.settings)
            api = ctx.learning(version=1)
            try:
                install_review_policy(api)
            except Exception:
                api.close()
                raise
            return run(settings, api, review_writes)
        finally:
            registration.dispose()

    ctx.register_cli_command("learning-bridge", "Run the project-scoped durable learning bridge",
                             setup_fn=setup, handler_fn=serve)
