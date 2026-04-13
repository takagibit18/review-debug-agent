"""CLI entry point for the Code Review & Debug Agent.

Provides ``review`` and ``debug`` subcommands via Click.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

import click

from src import __version__
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.orchestrator.agent_loop import AgentOrchestrator

T = TypeVar("T")


@click.group()
@click.version_option(version=__version__, prog_name="cr-debug-agent")
@click.option(
    "--model",
    default=None,
    help="Override the model name (defaults to MODEL_NAME env var).",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output.")
@click.option(
    "--permission-mode",
    type=click.Choice(["default", "plan"], case_sensitive=False),
    default=None,
    help="Permission mode override (default|plan).",
)
@click.pass_context
def main(
    ctx: click.Context,
    model: str | None,
    verbose: bool,
    permission_mode: str | None,
) -> None:
    """Code Review & Debug Agent — structured code review and debug assistance."""
    ctx.ensure_object(dict)
    ctx.obj["model"] = model
    ctx.obj["verbose"] = verbose
    ctx.obj["permission_mode"] = permission_mode.lower() if permission_mode else None


def _render_review_response(response: ReviewResponse, verbose: bool) -> None:
    """Render a review response for terminal output."""
    click.echo("Running review command...")
    click.echo(f"Run ID: {response.run_id}")
    click.echo(f"Summary: {response.report.summary}")
    click.echo(f"Issues: {len(response.report.issues)}")
    click.echo(f"Tracked files: {len(response.context.current_files)}")
    if verbose:
        click.echo(response.model_dump_json(indent=2))


def _render_debug_response(response: DebugResponse, verbose: bool) -> None:
    """Render a debug response for terminal output."""
    click.echo("Running debug command...")
    click.echo(f"Run ID: {response.run_id}")
    click.echo(f"Summary: {response.summary}")
    click.echo(f"Hypotheses: {len(response.hypotheses)}")
    click.echo(f"Steps: {len(response.steps)}")
    click.echo(f"Tracked files: {len(response.context.current_files)}")
    if verbose:
        click.echo(response.model_dump_json(indent=2))


def _run_async_command(
    operation: Coroutine[Any, Any, T],
    command_name: str,
) -> T:
    """Run one async CLI operation and convert failures into user-facing errors."""
    try:
        return asyncio.run(operation)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"{command_name} failed: {exc}") from exc


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option(
    "--diff", is_flag=True, help="Analyse staged git diff instead of full files."
)
@click.pass_context
def review(ctx: click.Context, path: str, diff: bool) -> None:
    """Run a structured code review on the target path or diff."""
    request = ReviewRequest(
        repo_path=path,
        diff_mode=diff,
        model_name=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
    )
    orchestrator = AgentOrchestrator(permission_mode=ctx.obj["permission_mode"])
    response = _run_async_command(orchestrator.run_review(request), "review")
    _render_review_response(response, ctx.obj["verbose"])


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option(
    "--error-log", type=click.Path(exists=True), help="Path to error log file."
)
@click.pass_context
def debug(ctx: click.Context, path: str, error_log: str | None) -> None:
    """Analyse a codebase to locate and suggest fixes for bugs."""
    request = DebugRequest(
        repo_path=path,
        error_log_path=error_log,
        model_name=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
    )
    orchestrator = AgentOrchestrator(permission_mode=ctx.obj["permission_mode"])
    response = _run_async_command(orchestrator.run_debug(request), "debug")
    _render_debug_response(response, ctx.obj["verbose"])


if __name__ == "__main__":
    main()
