"""CLI entry point for the Code Review & Debug Agent.

Provides ``review`` and ``debug`` subcommands via Click.
"""

import click

from src import __version__


@click.group()
@click.version_option(version=__version__, prog_name="cr-debug-agent")
@click.option(
    "--model",
    default=None,
    help="Override the model name (defaults to MODEL_NAME env var).",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output.")
@click.pass_context
def main(ctx: click.Context, model: str | None, verbose: bool) -> None:
    """Code Review & Debug Agent — structured code review and debug assistance."""
    ctx.ensure_object(dict)
    ctx.obj["model"] = model
    ctx.obj["verbose"] = verbose


@main.command()
@click.argument("path", default=".")
@click.option(
    "--diff", is_flag=True, help="Analyse staged git diff instead of full files."
)
@click.pass_context
def review(ctx: click.Context, path: str, diff: bool) -> None:
    """Run a structured code review on the target path or diff."""
    click.echo(f"[review] target={path} diff={diff} (not yet implemented)")


@main.command()
@click.argument("path", default=".")
@click.option(
    "--error-log", type=click.Path(exists=True), help="Path to error log file."
)
@click.pass_context
def debug(ctx: click.Context, path: str, error_log: str | None) -> None:
    """Analyse a codebase to locate and suggest fixes for bugs."""
    click.echo(f"[debug] target={path} error_log={error_log} (not yet implemented)")


if __name__ == "__main__":
    main()
