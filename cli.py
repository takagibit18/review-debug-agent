"""CLI entry point for the Code Review & Debug Agent.

Provides ``review`` and ``debug`` subcommands via Click.
"""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import click

from src.analyzer.output_formatter import ReviewIssue, triage_review_report
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
    triage = triage_review_report(response.report)
    click.echo("Running review command...")
    click.echo(f"Run ID: {response.run_id}")
    click.echo(f"Summary: {response.report.summary}")
    click.echo(f"Issues: {len(response.report.issues)}")
    click.echo("Immediate attention: " + ("yes" if triage.must_fix_critical else "no"))
    click.echo(f"Must-fix critical bugs: {len(triage.must_fix_critical)}")
    click.echo(f"Other bug findings: {len(triage.other_bug_findings)}")
    click.echo(
        f"Optimization suggestions: {len(triage.optimization_suggestions)}"
    )
    click.echo(f"Tracked files: {len(response.context.current_files)}")
    _render_run_diagnostics(response.context.run_diagnostics)
    if triage.must_fix_critical:
        click.secho("Must-Fix Critical Bugs:", fg="red", bold=True)
        for index, issue in enumerate(triage.must_fix_critical, start=1):
            _render_review_issue(issue, index)
    if triage.other_bug_findings:
        click.secho("Other Bug Findings:", fg="yellow", bold=True)
        for index, issue in enumerate(triage.other_bug_findings, start=1):
            _render_review_issue(issue, index)
    if triage.optimization_suggestions:
        click.secho("Optimization Suggestions:", fg="cyan", bold=True)
        for index, issue in enumerate(triage.optimization_suggestions, start=1):
            _render_review_issue(issue, index)
    if verbose:
        click.echo(response.model_dump_json(indent=2))


def _render_review_issue(issue: ReviewIssue, index: int) -> None:
    """Render one review issue in a compact human-readable form."""
    click.echo(
        f"{index}. [{issue.severity.value}] {issue.location} "
        f"(confidence={issue.confidence:.2f})"
    )
    click.echo(f"   Evidence: {issue.evidence}")
    click.echo(f"   Suggested fix: {issue.suggestion}")


def _render_debug_response(response: DebugResponse, verbose: bool) -> None:
    """Render a debug response for terminal output."""
    click.echo("Running debug command...")
    click.echo(f"Run ID: {response.run_id}")
    click.echo(f"Summary: {response.summary}")
    click.echo(f"Hypotheses: {len(response.hypotheses)}")
    click.echo(f"Steps: {len(response.steps)}")
    click.echo(f"Tracked files: {len(response.context.current_files)}")
    _render_run_diagnostics(response.context.run_diagnostics)
    if verbose:
        click.echo(response.model_dump_json(indent=2))


def _render_run_diagnostics(diagnostics: Any | None) -> None:
    """Render human-readable stop/degradation details when a run was not clean."""
    if diagnostics is None:
        return
    if diagnostics.status == "completed" and not diagnostics.reasons:
        return
    color = "red" if diagnostics.status == "degraded" else "yellow"
    click.secho(f"Status: {diagnostics.status}", fg=color, bold=True)
    if diagnostics.headline:
        click.echo(f"Why this ended: {diagnostics.headline}")
    if diagnostics.stop_reason:
        click.echo(f"Stop reason: {diagnostics.stop_reason}")
    for reason in diagnostics.reasons:
        click.echo(f"- {reason}")
    validation_error = (
        diagnostics.submit_review_validation_error
        or diagnostics.submit_debug_validation_error
    )
    if validation_error:
        click.echo(f"Validation detail: {_preview_text(validation_error, 180)}")


def _preview_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _load_text_file(path: str) -> str:
    """Read a UTF-8 text file for CLI-provided payloads such as PR diffs."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


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
@click.option(
    "--diff-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a unified diff file. Useful for PR automation in CI.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit only the structured JSON response.",
)
@click.pass_context
def review(
    ctx: click.Context,
    path: str,
    diff: bool,
    diff_file: str | None,
    json_output: bool,
) -> None:
    """Run a structured code review on the target path or diff."""
    diff_text = _load_text_file(diff_file) if diff_file else None
    request = ReviewRequest(
        repo_path=path,
        diff_mode=diff or diff_file is not None,
        diff_text=diff_text,
        model_name=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
    )
    orchestrator = AgentOrchestrator(permission_mode=ctx.obj["permission_mode"])
    response = _run_async_command(orchestrator.run_review(request), "review")
    if json_output:
        click.echo(response.model_dump_json(indent=2))
        return
    _render_review_response(response, ctx.obj["verbose"])


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option(
    "--error-log", type=click.Path(exists=True), help="Path to error log file."
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit only the structured JSON response.",
)
@click.pass_context
def debug(
    ctx: click.Context,
    path: str,
    error_log: str | None,
    json_output: bool,
) -> None:
    """Analyse a codebase to locate and suggest fixes for bugs."""
    request = DebugRequest(
        repo_path=path,
        error_log_path=error_log,
        model_name=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
    )
    orchestrator = AgentOrchestrator(permission_mode=ctx.obj["permission_mode"])
    response = _run_async_command(orchestrator.run_debug(request), "debug")
    if json_output:
        click.echo(response.model_dump_json(indent=2))
        return
    _render_debug_response(response, ctx.obj["verbose"])


if __name__ == "__main__":
    main()
