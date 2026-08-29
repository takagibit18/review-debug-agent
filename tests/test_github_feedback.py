"""Offline tests for explicit GitHub review feedback ingestion."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.analyzer.review_lifecycle import FeedbackStore
from src.integrations.github_feedback import (
    ingest_github_feedback,
    ingest_github_review_comments,
    parse_feedback_command,
)
from src.integrations.github_publisher import GITHUB_COMMENT_MARKER


def _parent_body(finding_id: str = "F-02") -> str:
    metadata = json.dumps(
        {
            "fingerprint": "finding-fingerprint",
            "finding_id": finding_id,
            "head_sha": "abc123",
            "run_id": "run-gh",
            "tool": "mergewarden",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "\n\n".join(
        [
            "**WARNING** confidence=0.90",
            "Evidence: the callback updates shared state.",
            "Suggestion: verify the execution contract.",
            GITHUB_COMMENT_MARKER,
            f"<!-- mergewarden:{metadata} -->",
        ]
    )


def _comments(
    reply_body: str,
    *,
    parent_body: str | None = None,
    reply_user: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "id": 100,
            "body": parent_body if parent_body is not None else _parent_body(),
            "user": {"login": "mergewarden[bot]", "type": "Bot"},
        },
        {
            "id": 200,
            "in_reply_to_id": 100,
            "body": reply_body,
            "user": reply_user or {"login": "alice", "type": "User"},
        },
    ]


def test_valid_feedback_is_bound_to_parent_finding_and_saved(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments("/mw-feedback valid\nThis finding is correct because the guard is required."),
        feedback_store=store,
    )

    assert result.imported == 1
    record = store.read()[0]
    assert record.id == "github-review-comment-200"
    assert record.finding_id == "F-02"
    assert record.human_verdict == "valid"
    assert record.human_reason == "This finding is correct because the guard is required."
    assert "Evidence: the callback" in record.finding_reasoning
    assert "mergewarden:" not in record.finding_reasoning
    assert GITHUB_COMMENT_MARKER not in record.finding_reasoning


def test_invalid_feedback_preserves_verdict_and_reason(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments(
            "/mw-feedback invalid\nThis path is serialized, so the race cannot occur."
        ),
        feedback_store=store,
    )

    assert result.imported == 1
    record = store.read()[0]
    assert record.human_verdict == "invalid"
    assert record.human_reason == (
        "This path is serialized, so the race cannot occur."
    )


def test_regular_reply_is_ignored(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments("Thanks, fixed."),
        feedback_store=store,
    )

    assert result.imported == 0
    assert result.ignored == 2
    assert store.read() == []


def test_feedback_on_foreign_parent_is_ignored(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments(
            "/mw-feedback invalid\nThis is not a MergeWarden finding.",
            parent_body="A developer comment without MergeWarden metadata.",
        ),
        feedback_store=store,
    )

    assert result.imported == 0
    assert result.ignored == 2
    assert store.read() == []


def test_feedback_on_legacy_mergewarden_comment_without_finding_id_is_ignored(
    tmp_path: Path,
) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments(
            "/mw-feedback valid\nThe old finding has no stable binding.",
            parent_body=_parent_body(finding_id=""),
        ),
        feedback_store=store,
    )

    assert result.imported == 0
    assert result.ignored == 2
    assert store.read() == []


def test_repeated_ingestion_is_idempotent(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    comments = _comments("/mw-feedback valid\nThe finding is correct.")

    first = ingest_github_review_comments(comments, feedback_store=store)
    second = ingest_github_review_comments(comments, feedback_store=store)

    assert first.imported == 1
    assert first.duplicates == 0
    assert second.imported == 0
    assert second.duplicates == 1
    assert len(store.read()) == 1


def test_malformed_commands_are_ignored_without_batch_failure(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    comments = [
        *_comments("/mw-feedback maybe\nNot a supported verdict."),
        {
            "id": 201,
            "in_reply_to_id": 100,
            "body": "/mw-feedback valid",
            "user": {"login": "bob", "type": "User"},
        },
    ]

    result = ingest_github_review_comments(comments, feedback_store=store)

    assert result.imported == 0
    assert result.ignored == 3
    assert store.read() == []


def test_bot_reply_is_not_human_feedback(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = ingest_github_review_comments(
        _comments(
            "/mw-feedback valid\nThis must not loop back into feedback.",
            reply_user={"login": "mergewarden[bot]", "type": "Bot"},
        ),
        feedback_store=store,
    )

    assert result.imported == 0
    assert result.ignored == 2
    assert store.read() == []


def test_parser_accepts_case_insensitive_inline_and_multiline_reasons() -> None:
    assert parse_feedback_command("/MW-FEEDBACK VALID\nReason text") is not None
    assert parse_feedback_command("/mw-feedback invalid inline reason") is not None
    assert parse_feedback_command("/mw-feedback maybe\nReason text") is None
    assert parse_feedback_command("/mw-feedback valid") is None


class FakeGitHubClient:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.calls: list[tuple[str, int]] = []

    async def list_review_comments(
        self,
        owner_repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((owner_repo, pr_number))
        return self.comments


def test_async_adapter_reuses_review_comment_client_contract(tmp_path: Path) -> None:
    client = FakeGitHubClient(
        _comments("/mw-feedback valid\nThe finding is correct.")
    )
    store = FeedbackStore(tmp_path / "feedback.jsonl")

    result = asyncio.run(
        ingest_github_feedback(
            client,
            owner_repo="owner/repo",
            pr_number=88,
            feedback_store=store,
        )
    )

    assert client.calls == [("owner/repo", 88)]
    assert result.imported == 1
