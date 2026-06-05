"""Tests for bridging GitHub PR events into review/publish."""

from __future__ import annotations

import asyncio

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import ReviewResponse
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    run_github_pull_request_review,
)


def test_pr_review_bridge_uses_auth_token_and_existing_review_flow(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.integrations import github_pr_review

    seen: dict[str, object] = {}

    class FakeAuthProvider:
        async def get_token(self, installation_id=None):  # type: ignore[no-untyped-def]
            seen["installation_id"] = installation_id
            return "installation-token"

    class FakeGitHubClient:
        def __init__(self, token: str) -> None:
            seen["token"] = token

        async def get_pull_request(self, owner_repo: str, pr_number: int):  # type: ignore[no-untyped-def]
            seen["get_pr"] = (owner_repo, pr_number)
            return {"head": {"sha": "head-sha-from-api"}}

        async def get_pull_diff(self, owner_repo: str, pr_number: int) -> str:
            seen["get_diff"] = (owner_repo, pr_number)
            return (
                "diff --git a/src/app.py b/src/app.py\n"
                "--- a/src/app.py\n"
                "+++ b/src/app.py\n"
                "@@ -1,1 +1,2 @@\n"
                " old\n"
                "+new\n"
            )

        async def close(self) -> None:
            seen["closed"] = True

    class FakeOrchestrator:
        async def run_review(self, request):  # type: ignore[no-untyped-def]
            seen["review_request"] = request
            return ReviewResponse(
                run_id="run-pr-review",
                report=ReviewReport(summary="ok"),
                context=ContextState(current_files=["."]),
            )

    class FakePublisher:
        def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
            seen["publisher_client"] = client

        async def publish(self, request):  # type: ignore[no-untyped-def]
            seen["publish_request"] = request
            return type(
                "PublishResult",
                (),
                {
                    "status": "published",
                    "lifecycle_plan": type(
                        "Lifecycle",
                        (),
                        {
                            "create_count": 0,
                            "update_count": 0,
                            "summary_only_count": 0,
                        },
                    )(),
                    "inline_comment_records": [],
                    "check_run": {"id": 99},
                },
            )()

    monkeypatch.setattr(github_pr_review, "GitHubApiClient", FakeGitHubClient)
    monkeypatch.setattr(github_pr_review, "AgentOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(github_pr_review, "GitHubPublisher", FakePublisher)

    result = asyncio.run(
        run_github_pull_request_review(
            GitHubPullRequestReviewTrigger(
                owner_repo="owner/repo",
                pull_number=7,
                head_sha="head-sha",
                base_sha="base-sha",
                installation_id=123,
                delivery_id="delivery",
                action="opened",
            ),
            auth_provider=FakeAuthProvider(),
        )
    )

    assert seen["installation_id"] == 123
    assert seen["token"] == "installation-token"
    assert seen["get_pr"] == ("owner/repo", 7)
    assert seen["get_diff"] == ("owner/repo", 7)
    assert seen["review_request"].diff_mode is True  # type: ignore[union-attr]
    assert seen["review_request"].diff_text.startswith("diff --git")  # type: ignore[union-attr]
    assert seen["publish_request"].head_sha == "head-sha-from-api"  # type: ignore[union-attr]
    assert seen["publish_request"].changed_lines == {"src/app.py": [2]}  # type: ignore[union-attr]
    assert seen["closed"] is True
    assert result.status == "published"
    assert result.check_run_id == 99
