"""Only this perimeter plugin knows the Codex-specific bridge package."""


def register(ctx):
    def setup(parser):
        parser.add_argument("--settings", required=True)

    def serve(args):
        from hermes_codex_learning.config import Settings
        from hermes_codex_learning.daemon import run
        return run(Settings.load(args.settings), ctx.learning(version=1))

    ctx.register_cli_command("learning-bridge", "Run the project-scoped durable learning bridge",
                             setup_fn=setup, handler_fn=serve)
