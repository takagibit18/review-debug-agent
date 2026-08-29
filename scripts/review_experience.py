"""CLI for recording human review experience and managing learned skills."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyzer.review_lifecycle import (  # noqa: E402
    FeedbackStore,
    PromptImprover,
    SkillStore,
    StaticImprover,
    propose_skill,
)
from src.analyzer.review_improver import complete_with_model  # noqa: E402
from src.integrations.github_feedback import (  # noqa: E402
    GitHubFeedbackIngestionResult,
    ingest_github_feedback,
)
from src.integrations.github_publisher import (  # noqa: E402
    GitHubApiClient,
    resolve_github_token,
)
from src.models.exceptions import ModelClientError  # noqa: E402


def _default_path(name: str) -> Path:
    return Path.cwd() / name


def _read_json_value(value: str) -> dict[str, Any]:
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.exists() else value
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("proposal JSON must be an object")
    return parsed


@click.group()
def cli() -> None:
    """Record feedback and move review skills through their lifecycle."""


@cli.command()
@click.option("--feedback-file", type=click.Path(path_type=Path), default=None)
@click.option("--id", "feedback_id", required=True)
@click.option("--finding-id", required=True)
@click.option("--human-verdict", required=True)
@click.option("--human-reason", required=True)
@click.option("--finding-summary", required=True)
@click.option("--finding-reasoning", required=True)
def record(
    feedback_file: Path | None,
    feedback_id: str,
    finding_id: str,
    human_verdict: str,
    human_reason: str,
    finding_summary: str,
    finding_reasoning: str,
) -> None:
    """Append one compact human feedback record."""

    try:
        saved = FeedbackStore(feedback_file or _default_path("review_experience/feedback.jsonl")).append(
            {
                "id": feedback_id,
                "finding_id": finding_id,
                "human_verdict": human_verdict,
                "human_reason": human_reason,
                "finding_summary": finding_summary,
                "finding_reasoning": finding_reasoning,
            }
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"recorded {saved.id}")


@cli.command("ingest-github")
@click.option("--repo", required=True)
@click.option("--pr", "pr_number", type=click.IntRange(min=1), required=True)
@click.option("--feedback-file", type=click.Path(path_type=Path), default=None)
def ingest_github(repo: str, pr_number: int, feedback_file: Path | None) -> None:
    """Import explicit human feedback from GitHub review comment replies."""
    token = resolve_github_token()
    if not token:
        raise click.ClickException("GITHUB_TOKEN/GH_TOKEN is required for GitHub ingestion.")

    async def _run() -> GitHubFeedbackIngestionResult:
        client = GitHubApiClient(token)
        try:
            return await ingest_github_feedback(
                client,
                owner_repo=repo,
                pr_number=pr_number,
                feedback_store=FeedbackStore(
                    feedback_file or _default_path("review_experience/feedback.jsonl")
                ),
            )
        finally:
            await client.close()

    try:
        result = asyncio.run(_run())
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo("GitHub feedback ingestion complete:")
    click.echo(f"  imported: {result.imported}")
    click.echo(f"  duplicate: {result.duplicates}")
    click.echo(f"  ignored: {result.ignored}")


@cli.command()
@click.option("--feedback-file", type=click.Path(path_type=Path), default=None)
@click.option("--skills-file", type=click.Path(path_type=Path), default=None)
@click.option(
    "--proposal-json",
    default=None,
    help="Offline Improver response as JSON or a JSON file path.",
)
@click.option(
    "--model",
    "use_model",
    is_flag=True,
    help="Generate the proposal with the configured ModelClient.",
)
def propose(
    feedback_file: Path | None,
    skills_file: Path | None,
    proposal_json: str | None,
    use_model: bool,
) -> None:
    """Create one candidate skill from detailed feedback."""

    if use_model == (proposal_json is not None):
        raise click.UsageError(
            "Exactly one of --model or --proposal-json must be provided."
        )

    try:
        improver = (
            PromptImprover(complete_with_model)
            if use_model
            else StaticImprover(_read_json_value(proposal_json or ""))
        )
        skill = propose_skill(
            FeedbackStore(
                feedback_file or _default_path("review_experience/feedback.jsonl")
            ),
            SkillStore(skills_file or _default_path("review_skills/learned.jsonl")),
            improver,
        )
    except (OSError, json.JSONDecodeError, ModelClientError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created {skill.id} (candidate)")


@cli.command()
@click.argument("skill_id")
@click.option("--skills-file", type=click.Path(path_type=Path), default=None)
def activate(skill_id: str, skills_file: Path | None) -> None:
    """Move a candidate skill to active after human approval."""

    try:
        skill = SkillStore(
            skills_file or _default_path("review_skills/learned.jsonl")
        ).update_status(skill_id, "active")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"activated {skill.id}")


@cli.command()
@click.argument("skill_id")
@click.option("--skills-file", type=click.Path(path_type=Path), default=None)
def deprecate(skill_id: str, skills_file: Path | None) -> None:
    """Move an active skill to deprecated while retaining its record."""

    try:
        skill = SkillStore(
            skills_file or _default_path("review_skills/learned.jsonl")
        ).update_status(skill_id, "deprecated")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"deprecated {skill.id}")


if __name__ == "__main__":
    cli()
