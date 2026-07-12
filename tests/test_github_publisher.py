"""Tests for GitHub advisory publishing and comment lifecycle planning."""

from __future__ import annotations

import json
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse
from src.integrations.github_publisher import (
    GITHUB_COMMENT_MARKER,
    GitHubPublishRequest,
    GitHubPublisher,
    PublishedCommentRecord,
    build_comment_lifecycle_plan,
    extract_comment_metadata,
)


class RecordingGitHubClient:
    def __init__(
        self,
        existing_comments: list[dict[str, Any]] | None = None,
        existing_check_runs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.existing_comments = existing_comments or []
        self.existing_check_runs = existing_check_runs or []
        self.created_check_runs: list[dict[str, Any]] = []
        self.updated_check_runs: list[dict[str, Any]] = []
        self.created_review_comments: list[dict[str, Any]] = []
        self.updated_review_comments: list[dict[str, Any]] = []

    async def list_review_comments(self, owner_repo: str, pr_number: int) -> list[dict[str, Any]]:
        assert owner_repo == "owner/repo"
        assert pr_number == 7
        return self.existing_comments

    async def create_check_run(self, owner_repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_check_runs.append({"owner_repo": owner_repo, "payload": payload})
        return {"id": 101, "html_url": "https://github.example/check/101"}

    async def list_check_runs(
        self,
        owner_repo: str,
        head_sha: str,
        check_name: str,
    ) -> list[dict[str, Any]]:
        return self.existing_check_runs

    async def update_check_run(
        self,
        owner_repo: str,
        check_run_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.updated_check_runs.append(
            {"owner_repo": owner_repo, "check_run_id": check_run_id, "payload": payload}
        )
        return {"id": check_run_id, "html_url": f"https://github.example/check/{check_run_id}"}

    async def create_review_comment(
        self,
        owner_repo: str,
        pr_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.created_review_comments.append(
            {"owner_repo": owner_repo, "pr_number": pr_number, "payload": payload}
        )
        return {"id": 201, "html_url": "https://github.example/comment/201"}

    async def update_review_comment(
        self,
        owner_repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        self.updated_review_comments.append(
            {"owner_repo": owner_repo, "comment_id": comment_id, "body": body}
        )
        return {"id": comment_id, "html_url": f"https://github.example/comment/{comment_id}"}


def _response() -> ReviewResponse:
    return ReviewResponse(
        run_id="run-gh",
        report=ReviewReport(
            summary="review summary",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/app.py:10",
                    evidence="+ risky_call()",
                    suggestion="Guard the call.",
                    confidence=0.9,
                ),
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location="src/other.py:5",
                    evidence="+ allow_all = True",
                    suggestion="Restore authorization.",
                    confidence=0.95,
                ),
            ],
        ),
        context=ContextState(),
    )


def _request() -> GitHubPublishRequest:
    return GitHubPublishRequest(
        owner_repo="owner/repo",
        pr_number=7,
        head_sha="abc123",
        response=_response(),
        changed_lines={"src/app.py": [10]},
        dry_run=True,
    )


def test_publisher_dry_run_builds_complete_plan_without_network_calls() -> None:
    client = RecordingGitHubClient()
    publisher = GitHubPublisher(client=client)

    result = publisher.publish_sync(_request())

    assert result.status == "dry_run"
    assert result.check_run["conclusion"] == "neutral"
    assert result.lifecycle_plan.create_count == 1
    assert result.lifecycle_plan.summary_only_count == 1
    assert result.inline_comment_records == []
    assert client.created_check_runs == []
    assert client.created_review_comments == []
    assert GITHUB_COMMENT_MARKER in result.lifecycle_plan.create[0].body
    assert "fingerprint" in result.lifecycle_plan.create[0].body


def test_comment_lifecycle_updates_existing_and_stales_missing_mergewarden_comments() -> None:
    first = _request()
    fresh = GitHubPublisher(client=RecordingGitHubClient()).build_publish_plan(first)
    active = fresh.lifecycle_plan.create[0]
    existing_active_body = active.body.replace("run-gh", "older-run")
    stale_body = "\n".join(
        [
            "Old body",
            GITHUB_COMMENT_MARKER,
            "<!-- mergewarden:{\"run_id\":\"older\",\"fingerprint\":\"stale-fp\",\"head_sha\":\"old\"} -->",
        ]
    )
    foreign = {"id": 99, "body": "manual reviewer comment"}

    plan = build_comment_lifecycle_plan(
        candidates=[active],
        existing_comments=[
            {"id": 11, "body": existing_active_body, "path": "src/app.py", "line": 10},
            {"id": 12, "body": stale_body, "path": "src/app.py", "line": 12},
            foreign,
        ],
        run_id="run-gh",
        head_sha="abc123",
    )

    assert plan.create == []
    assert [item.comment_id for item in plan.update] == [11]
    assert [item.comment_id for item in plan.stale] == [12]
    assert plan.foreign_comment_count == 1


def test_real_publish_uses_fake_client_for_check_comments_and_stale_updates() -> None:
    existing_stale = {
        "id": 12,
        "body": "\n".join(
            [
                "Old body",
                GITHUB_COMMENT_MARKER,
                "<!-- mergewarden:{\"run_id\":\"older\",\"fingerprint\":\"stale-fp\",\"head_sha\":\"old\"} -->",
            ]
        ),
        "path": "src/app.py",
        "line": 12,
    }
    client = RecordingGitHubClient(existing_comments=[existing_stale])
    publisher = GitHubPublisher(client=client)
    request = _request().model_copy(update={"dry_run": False})

    result = publisher.publish_sync(request)

    assert result.status == "published"
    assert result.check_run["id"] == 101
    assert len(client.created_check_runs) == 1
    assert len(client.created_review_comments) == 1
    assert client.created_review_comments[0]["payload"]["commit_id"] == "abc123"
    assert client.created_review_comments[0]["payload"]["path"] == "src/app.py"
    assert client.created_review_comments[0]["payload"]["line"] == 10
    assert client.created_review_comments[0]["payload"]["side"] == "RIGHT"
    assert len(client.updated_review_comments) == 1
    assert "Stale MergeWarden advisory" in client.updated_review_comments[0]["body"]
    assert result.inline_comment_records[0].comment_id == 201
    assert isinstance(result.inline_comment_records[0], PublishedCommentRecord)


def test_recovered_publish_updates_matching_check_instead_of_creating_duplicate() -> None:
    client = RecordingGitHubClient(
        existing_check_runs=[
            {
                "id": 77,
                "name": "MergeWarden advisory",
                "head_sha": "abc123",
                "external_id": "mergewarden:owner/repo:7:abc123",
            }
        ]
    )
    request = _request().model_copy(update={"dry_run": False, "publish_comments": False})

    result = GitHubPublisher(client=client).publish_sync(request)

    assert result.check_run["id"] == 77
    assert client.created_check_runs == []
    assert len(client.updated_check_runs) == 1
    assert client.updated_check_runs[0]["payload"]["external_id"] == (
        "mergewarden:owner/repo:7:abc123"
    )


def test_extract_comment_metadata_ignores_foreign_and_invalid_comments() -> None:
    assert extract_comment_metadata("manual reviewer comment") is None
    assert extract_comment_metadata(f"{GITHUB_COMMENT_MARKER}\n<!-- mergewarden:not-json -->") is None
    parsed = extract_comment_metadata(
        f"{GITHUB_COMMENT_MARKER}\n<!-- mergewarden:{json.dumps({'fingerprint': 'fp'})} -->"
    )
    assert parsed is not None
    assert parsed.fingerprint == "fp"
