"""Bridge GitHub pull request events into the existing review/publish flow."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.review_failures import find_blocking_review_error
from src.analyzer.schemas import ReviewRequest, ReviewResponse
from src.config import get_settings
from src.integrations.github_auth import GitHubAuthProvider, get_github_auth_provider
from src.integrations.github_publisher import (
    GitHubApiClient,
    GitHubPublishRequest,
    GitHubPublisher,
)
from src.integrations.github_workspace import (
    GitHubRepositoryWorkspace,
    materialize_github_workspace,
)
from src.orchestrator.agent_loop import AgentOrchestrator

logger = logging.getLogger(__name__)


class GitHubPullRequestReviewTrigger(BaseModel):
    """Normalized webhook data needed to review a GitHub pull request."""

    owner_repo: str = Field(min_length=1)
    pull_number: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    installation_id: int | None = None
    trigger_event: str = "pull_request"
    delivery_id: str = ""
    action: str = ""


class GitHubPullRequestReviewResult(BaseModel):
    """Compact result for webhook logging and tests."""

    status: str
    owner_repo: str
    pull_number: int
    head_sha: str
    run_id: str = ""
    issues_count: int = 0
    inline_comments_count: int = 0
    summary_only_count: int = 0
    check_run_id: int | None = None


class GitHubPullRequestReviewExecution(BaseModel):
    """Full execution payload used by the platform worker for artifact capture."""

    result: GitHubPullRequestReviewResult
    review_response: ReviewResponse
    publish_result: Any
    diff_text: str = ""
    changed_lines: dict[str, list[int]]
    event_log_path: str = ""


async def run_github_pull_request_review(
    trigger: GitHubPullRequestReviewTrigger,
    *,
    auth_provider: GitHubAuthProvider | None = None,
) -> GitHubPullRequestReviewResult:
    """Review and publish one GitHub pull request using the configured auth mode."""
    execution = await execute_github_pull_request_review(
        trigger,
        auth_provider=auth_provider,
    )
    return execution.result


async def execute_github_pull_request_review(
    trigger: GitHubPullRequestReviewTrigger,
    *,
    auth_provider: GitHubAuthProvider | None = None,
    publish_comments: bool = True,
    model_name: str | None = None,
) -> GitHubPullRequestReviewExecution:
    """Run the GitHub PR review flow and return full artifacts for persistence."""
    provider = auth_provider or get_github_auth_provider()
    token = await provider.get_token(trigger.installation_id)
    client = GitHubApiClient(token)
    try:
        logger.info(
            "pull_request review started",
            extra=_log_context(trigger),
        )
        diff_text = await client.get_pull_diff(trigger.owner_repo, trigger.pull_number)
        changed_lines = {
            path: sorted(lines)
            for path, lines in changed_new_lines_by_file(diff_text).items()
        }
        logger.info(
            "diff fetched",
            extra={
                **_log_context(trigger),
                "diff_bytes": len(diff_text.encode("utf-8")),
                "changed_file_count": len(changed_lines),
            },
        )

        async with _materialize_review_workspace(trigger, token) as workspace:
            response = await AgentOrchestrator().run_review(
                ReviewRequest(
                    repo_path=str(workspace.path),
                    diff_mode=True,
                    diff_text=diff_text,
                    model_name=model_name,
                )
            )
            event_log_path = _preserve_event_log(workspace.path, response.run_id)
            blocking_error = find_blocking_review_error(response)
            if blocking_error:
                raise RuntimeError(f"review produced no trusted result: {blocking_error}")

            publish_result = await GitHubPublisher(client).publish(
                GitHubPublishRequest(
                    owner_repo=trigger.owner_repo,
                    pr_number=trigger.pull_number,
                    head_sha=workspace.head_sha,
                    response=response,
                    changed_lines=changed_lines,
                    dry_run=False,
                    publish_comments=publish_comments,
                )
            )
            logger.info(
                "comment published",
                extra={
                    **_log_context(trigger),
                    "run_id": response.run_id,
                    "inline_comment_records": len(publish_result.inline_comment_records),
                },
            )
            result = GitHubPullRequestReviewResult(
                status=publish_result.status,
                owner_repo=trigger.owner_repo,
                pull_number=trigger.pull_number,
                head_sha=workspace.head_sha,
                run_id=response.run_id,
                issues_count=len(response.report.issues),
                inline_comments_count=publish_result.lifecycle_plan.create_count
                + publish_result.lifecycle_plan.update_count,
                summary_only_count=publish_result.lifecycle_plan.summary_only_count,
                check_run_id=_int_or_none(publish_result.check_run.get("id")),
            )
            logger.info(
                "review completed",
                extra={
                    **_log_context(trigger),
                    "run_id": response.run_id,
                    "issues_count": result.issues_count,
                },
            )
            return GitHubPullRequestReviewExecution(
                result=result,
                review_response=response,
                publish_result=publish_result,
                diff_text=diff_text,
                changed_lines=changed_lines,
                event_log_path=event_log_path,
            )
    except Exception:
        logger.exception(
            "review failed",
            extra=_log_context(trigger),
        )
        raise
    finally:
        await client.close()


@asynccontextmanager
async def _materialize_review_workspace(
    trigger: GitHubPullRequestReviewTrigger,
    token: str,
) -> AsyncIterator[GitHubRepositoryWorkspace]:
    """Keep blocking Git operations and cleanup off the worker event loop."""
    manager = materialize_github_workspace(
        owner_repo=trigger.owner_repo,
        pull_number=trigger.pull_number,
        head_sha=trigger.head_sha,
        token=token,
    )
    workspace = await asyncio.to_thread(manager.__enter__)
    try:
        yield workspace
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


def _preserve_event_log(workspace_path: Path, run_id: str) -> str:
    """Keep the existing event-log artifact before the repository is removed."""
    configured_dir = Path(get_settings().event_log_dir)
    log_name = f"{run_id}.jsonl"
    if configured_dir.is_absolute():
        path = configured_dir / log_name
        return str(path) if path.exists() else ""

    source = workspace_path / configured_dir / log_name
    if not source.exists():
        return ""
    destination = (Path.cwd() / configured_dir / log_name).resolve()
    if source.resolve() == destination:
        return str(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return str(destination)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed or None


def _log_context(trigger: GitHubPullRequestReviewTrigger) -> dict[str, object]:
    return {
        "delivery_id": trigger.delivery_id,
        "owner_repo": trigger.owner_repo,
        "pull_number": trigger.pull_number,
        "head_sha": trigger.head_sha,
        "installation_id": trigger.installation_id,
        "trigger_event": trigger.trigger_event,
        "action": trigger.action,
    }
