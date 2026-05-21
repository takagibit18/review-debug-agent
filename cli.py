"""CLI entry point for MergeWarden.

Provides ``review`` and ``debug`` subcommands via Click.
"""

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import click

from src.analyzer.output_formatter import ReviewIssue, triage_review_report
from src.analyzer.run_summary import summarize_run_artifacts
from src import __version__
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.integrations.github_adapter import build_github_advisory_payload
from src.integrations.github_publisher import (
    GitHubApiClient,
    GitHubPublishRequest,
    GitHubPublishResult,
    GitHubPublisher,
    GitHubPublisherClient,
    resolve_github_token,
)
from src.orchestrator.agent_loop import AgentOrchestrator

T = TypeVar("T")


@click.group()
@click.version_option(version=__version__, prog_name="mergewarden")
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
    """MergeWarden — AI PR gatekeeper for safer merges."""
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


async def _publish_and_close_client(
    publisher: GitHubPublisher,
    request: GitHubPublishRequest,
    client: GitHubPublisherClient,
) -> GitHubPublishResult:
    try:
        return await publisher.publish(request)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option(
    "--diff", is_flag=True, help="Analyse staged git diff instead of full files."
)
@click.option(
    "--output-json",
    default=None,
    type=click.Path(exists=False),
    help="Optional path to write the ReviewResponse JSON.",
)
@click.option(
    "--summary-json",
    default=None,
    type=click.Path(exists=False),
    help="Optional path to write a compact run artifact summary JSON.",
)
@click.pass_context
def review(
    ctx: click.Context,
    path: str,
    diff: bool,
    output_json: str | None,
    summary_json: str | None,
) -> None:
    """Run a structured code review on the target path or diff."""
    request = ReviewRequest(
        repo_path=path,
        diff_mode=diff,
        model_name=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
    )
    orchestrator = AgentOrchestrator(permission_mode=ctx.obj["permission_mode"])
    response = _run_async_command(orchestrator.run_review(request), "review")
    if output_json:
        Path(output_json).write_text(response.model_dump_json(indent=2) + "\n", encoding="utf-8")
        click.echo(f"Review response saved to: {Path(output_json).as_posix()}")
    if summary_json:
        event_log_path = _resolve_event_log_path(path, response.run_id)
        summary = summarize_run_artifacts(
            run_id=response.run_id,
            event_log_path=event_log_path,
            response_json_path=output_json,
            publish_status="not_requested",
        )
        Path(summary_json).write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
        click.echo(f"Run summary saved to: {Path(summary_json).as_posix()}")
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


@main.command("advisory-export")
@click.option(
    "--response-json",
    required=True,
    type=click.Path(exists=True),
    help="Path to a ReviewResponse JSON file.",
)
@click.option(
    "--changed-lines-json",
    required=True,
    type=click.Path(exists=True),
    help="JSON object mapping repo-relative paths to changed line numbers.",
)
@click.option(
    "--output-json",
    default=None,
    type=click.Path(exists=False),
    help="Optional output path. Defaults to stdout.",
)
def advisory_export(
    response_json: str,
    changed_lines_json: str,
    output_json: str | None,
) -> None:
    """Convert a review response into local GitHub advisory payload JSON."""
    response = ReviewResponse.model_validate_json(
        Path(response_json).read_text(encoding="utf-8")
    )
    changed_lines = _load_changed_lines(Path(changed_lines_json))
    payload = build_github_advisory_payload(response, changed_lines)
    output = payload.model_dump_json(indent=2)
    if output_json:
        Path(output_json).write_text(output + "\n", encoding="utf-8")
        click.echo(f"Advisory payload saved to: {Path(output_json).as_posix()}")
        return
    click.echo(output)


def _load_changed_lines(path: Path) -> dict[str, set[int]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise click.ClickException("changed-lines JSON must be an object.")
    changed: dict[str, set[int]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            raise click.ClickException(
                "changed-lines JSON values must be arrays of line numbers."
            )
        try:
            changed[str(key)] = {int(item) for item in value}
        except (TypeError, ValueError) as exc:
            raise click.ClickException(
                "changed-lines JSON values must contain only line numbers."
            ) from exc
    return changed


@main.group("github-advisory")
def github_advisory() -> None:
    """Publish MergeWarden advisory checks and PR comments."""


@github_advisory.command("publish")
@click.option(
    "--repo",
    "owner_repo",
    required=True,
    help="GitHub repository in owner/name form.",
)
@click.option(
    "--pr-number",
    required=True,
    type=int,
    help="Pull request number.",
)
@click.option(
    "--head-sha",
    required=True,
    help="Pull request head SHA used for the check run and metadata.",
)
@click.option(
    "--response-json",
    required=True,
    type=click.Path(exists=True),
    help="Path to a ReviewResponse JSON file.",
)
@click.option(
    "--changed-lines-json",
    required=True,
    type=click.Path(exists=True),
    help="JSON object mapping repo-relative paths to changed line numbers.",
)
@click.option(
    "--dry-run/--publish",
    default=None,
    help="Dry-run by default; pass --publish to write to GitHub.",
)
@click.option(
    "--token",
    default=None,
    help="GitHub token override. Defaults to GITHUB_TOKEN/GH_TOKEN.",
)
@click.option(
    "--output-json",
    default=None,
    type=click.Path(exists=False),
    help="Optional output path. Defaults to stdout.",
)
def github_advisory_publish(
    owner_repo: str,
    pr_number: int,
    head_sha: str,
    response_json: str,
    changed_lines_json: str,
    dry_run: bool,
    token: str | None,
    output_json: str | None,
) -> None:
    """Publish a GitHub soft check and advisory comments for a review response."""
    response = ReviewResponse.model_validate_json(
        Path(response_json).read_text(encoding="utf-8")
    )
    changed_lines = _load_changed_lines(Path(changed_lines_json))
    effective_dry_run = get_settings().github_advisory_dry_run if dry_run is None else dry_run
    request = GitHubPublishRequest(
        owner_repo=owner_repo,
        pr_number=pr_number,
        head_sha=head_sha,
        response=response,
        changed_lines={key: sorted(value) for key, value in changed_lines.items()},
        dry_run=effective_dry_run,
    )
    client: GitHubPublisherClient
    if effective_dry_run:
        client = _DryRunGithubClient()
    else:
        resolved_token = resolve_github_token(token)
        if not resolved_token:
            raise click.ClickException("GITHUB_TOKEN is required when using --publish.")
        client = GitHubApiClient(resolved_token)
    publisher = GitHubPublisher(client)
    result = _run_async_command(
        _publish_and_close_client(publisher, request, client),
        "github-advisory publish",
    )
    output = result.model_dump_json(indent=2)
    if output_json:
        Path(output_json).write_text(output + "\n", encoding="utf-8")
        click.echo(f"GitHub advisory publish result saved to: {Path(output_json).as_posix()}")
        return
    click.echo(output)


class _DryRunGithubClient:
    """Client placeholder; dry-runs never call network methods."""

    async def list_review_comments(self, owner_repo: str, pr_number: int) -> list[dict[str, Any]]:
        raise RuntimeError("dry-run should not list GitHub comments")

    async def create_check_run(self, owner_repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("dry-run should not create GitHub check runs")

    async def create_review_comment(
        self,
        owner_repo: str,
        pr_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise RuntimeError("dry-run should not create GitHub comments")

    async def update_review_comment(
        self,
        owner_repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        raise RuntimeError("dry-run should not update GitHub comments")


def _resolve_event_log_path(repo_path: str, run_id: str) -> Path:
    settings = get_settings()
    raw = Path(settings.event_log_dir)
    if raw.is_absolute():
        return raw / f"{run_id}.jsonl"
    return Path(repo_path) / raw / f"{run_id}.jsonl"


if __name__ == "__main__":
    main()
