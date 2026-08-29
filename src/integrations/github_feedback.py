"""Ingest explicit human feedback from GitHub review comment replies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.analyzer.review_lifecycle import FeedbackRecord, FeedbackStore
from src.integrations.github_publisher import (
    extract_comment_metadata,
    extract_public_comment_body,
)

_FEEDBACK_COMMAND_PATTERN = re.compile(
    r"^/mw-feedback[ \t]+(valid|invalid)(?:[ \t]+(.*))?$",
    re.IGNORECASE,
)
_MERGEWARDEN_LOGINS = {
    "mergewarden",
    "mergewarden-bot",
    "mergewarden[bot]",
    "mergewarden-bot[bot]",
}
_DUPLICATE_ERROR_PREFIX = "feedback id already exists:"


class GitHubReviewCommentsClient(Protocol):
    """Minimal GitHub client contract needed for feedback ingestion."""

    async def list_review_comments(
        self,
        owner_repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ParsedFeedbackCommand:
    """One valid explicit ``/mw-feedback`` command."""

    verdict: str
    reason: str


@dataclass(slots=True)
class GitHubFeedbackIngestionResult:
    """Counts produced by one deterministic ingestion pass."""

    imported: int = 0
    duplicates: int = 0
    ignored: int = 0


def parse_feedback_command(body: str) -> ParsedFeedbackCommand | None:
    """Parse the v1 explicit feedback command, returning ``None`` otherwise."""
    lines = body.splitlines()
    if not lines:
        return None

    match = _FEEDBACK_COMMAND_PATTERN.fullmatch(lines[0].strip())
    if match is None:
        return None

    reason_parts = []
    inline_reason = match.group(2)
    if inline_reason:
        reason_parts.append(inline_reason)
    reason_parts.extend(lines[1:])
    reason = "\n".join(reason_parts).strip()
    if not reason:
        return None

    return ParsedFeedbackCommand(
        verdict=match.group(1).lower(),
        reason=reason,
    )


async def ingest_github_feedback(
    client: GitHubReviewCommentsClient,
    *,
    owner_repo: str,
    pr_number: int,
    feedback_store: FeedbackStore,
) -> GitHubFeedbackIngestionResult:
    """Fetch one PR's review comments and append valid human corrections."""
    comments = await client.list_review_comments(owner_repo, pr_number)
    return ingest_github_review_comments(comments, feedback_store=feedback_store)


def ingest_github_review_comments(
    comments: Sequence[Mapping[str, Any]],
    *,
    feedback_store: FeedbackStore,
) -> GitHubFeedbackIngestionResult:
    """Convert raw review comments into existing ``FeedbackRecord`` entries."""
    result = GitHubFeedbackIngestionResult()
    comments_by_id = {
        comment_id: comment
        for comment in comments
        if (comment_id := _comment_id(comment.get("id")))
    }

    for reply in comments:
        parsed = parse_feedback_command(_comment_body(reply))
        reply_id = _comment_id(reply.get("id"))
        parent_id = _comment_id(reply.get("in_reply_to_id"))
        if (
            parsed is None
            or not reply_id
            or not parent_id
            or _is_bot_authored(reply)
        ):
            result.ignored += 1
            continue

        parent = comments_by_id.get(parent_id)
        if parent is None:
            result.ignored += 1
            continue

        metadata = extract_comment_metadata(_comment_body(parent))
        if (
            metadata is None
            or metadata.tool.strip().lower() != "mergewarden"
            or not metadata.finding_id.strip()
            or not _is_bot_authored(parent)
        ):
            result.ignored += 1
            continue

        finding_content = extract_public_comment_body(_comment_body(parent))
        if not finding_content:
            result.ignored += 1
            continue

        feedback_id = f"github-review-comment-{reply_id}"
        record = FeedbackRecord(
            id=feedback_id,
            finding_id=metadata.finding_id,
            human_verdict=parsed.verdict,
            human_reason=parsed.reason,
            finding_summary=finding_content,
            finding_reasoning=finding_content,
        )
        try:
            feedback_store.append(record)
        except ValueError as exc:
            if not str(exc).startswith(_DUPLICATE_ERROR_PREFIX):
                raise
            result.duplicates += 1
            continue
        result.imported += 1

    return result


def _comment_body(comment: Mapping[str, Any]) -> str:
    value = comment.get("body", "")
    return value if isinstance(value, str) else str(value or "")


def _comment_id(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _is_bot_authored(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    login = ""
    user_type = ""
    if isinstance(user, Mapping):
        login = str(user.get("login", "") or "").strip().lower()
        user_type = str(user.get("type", "") or "").strip().lower()

    association = str(comment.get("author_association", "") or "").strip().lower()
    return (
        user_type == "bot"
        or association == "bot"
        or login.endswith("[bot]")
        or login in _MERGEWARDEN_LOGINS
    )
