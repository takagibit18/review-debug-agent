"""Bridge GitHub pull request events into the existing review/publish flow."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.review_failures import find_blocking_review_error
from src.analyzer.schemas import ReviewRequest
from src.integrations.github_auth import GitHubAuthProvider, get_github_auth_provider
from src.integrations.github_publisher import GitHubApiClient, GitHubPublishRequest, GitHubPublisher
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


async def run_github_pull_request_review(
    trigger: GitHubPullRequestReviewTrigger,
    *,
    auth_provider: GitHubAuthProvider | None = None,
) -> GitHubPullRequestReviewResult:
    """Review and publish one GitHub pull request using the configured auth mode."""
    provider = auth_provider or get_github_auth_provider()
    token = await provider.get_token(trigger.installation_id)
    client = GitHubApiClient(token)
    try:
        logger.info(
            "pull_request review started",
            extra=_log_context(trigger),
        )
        pull_request = await client.get_pull_request(trigger.owner_repo, trigger.pull_number)
        head_sha = _payload_head_sha(pull_request) or trigger.head_sha
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

        response = await AgentOrchestrator().run_review(
            ReviewRequest(repo_path=".", diff_mode=True, diff_text=diff_text)
        )
        blocking_error = find_blocking_review_error(response)
        if blocking_error:
            raise RuntimeError(f"review produced no trusted result: {blocking_error}")

        publish_result = await GitHubPublisher(client).publish(
            GitHubPublishRequest(
                owner_repo=trigger.owner_repo,
                pr_number=trigger.pull_number,
                head_sha=head_sha,
                response=response,
                changed_lines=changed_lines,
                dry_run=False,
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
            head_sha=head_sha,
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
        return result
    except Exception:
        logger.exception(
            "review failed",
            extra=_log_context(trigger),
        )
        raise
    finally:
        await client.close()


def _payload_head_sha(payload: dict[str, object]) -> str:
    head = payload.get("head")
    if not isinstance(head, dict):
        return ""
    return str(head.get("sha", "") or "")


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
