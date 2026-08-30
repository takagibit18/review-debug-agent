"""Tests for bridging GitHub PR events into review/publish."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import ReviewResponse
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    run_github_pull_request_review,
)
from src.tools.exceptions import FileNotFoundToolError
from src.tools.file_read import FileReadTool
from src.tools.path_utils import tool_workspace_root


def test_pr_review_bridge_binds_existing_review_flow_to_target_workspace(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    from src.integrations import github_pr_review

    seen: dict[str, object] = {}
    backend_root = tmp_path / "mergewarden"
    workspace_root = tmp_path / "target-workspace"
    backend_root.mkdir()
    workspace_root.mkdir()
    (backend_root / "local_only.py").write_text("BACKEND_ONLY = True\n", encoding="utf-8")
    (workspace_root / "external_only.py").write_text(
        "EXTERNAL_ONLY = True\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(backend_root)

    class FakeAuthProvider:
        async def get_token(self, installation_id=None):  # type: ignore[no-untyped-def]
            seen["installation_id"] = installation_id
            return "installation-token"

    class FakeGitHubClient:
        def __init__(self, token: str) -> None:
            seen["token"] = token

        async def get_pull_diff(self, owner_repo: str, pr_number: int) -> str:
            raise AssertionError("live pull request diff must not be requested")

        async def close(self) -> None:
            seen["closed"] = True

    class FakeOrchestrator:
        async def run_review(self, request):  # type: ignore[no-untyped-def]
            seen["review_request"] = request
            with tool_workspace_root(request.repo_path):
                seen["external_read"] = await FileReadTool().execute(
                    file_path="external_only.py"
                )
                with pytest.raises(FileNotFoundToolError):
                    await FileReadTool().execute(file_path="local_only.py")
            event_log = Path(request.repo_path) / ".mergewarden" / "logs" / "run-pr-review.jsonl"
            event_log.parent.mkdir(parents=True)
            event_log.write_text('{"event_type":"test"}\n', encoding="utf-8")
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

    @contextmanager
    def fake_materialize_github_workspace(**kwargs):  # type: ignore[no-untyped-def]
        seen["workspace_args"] = kwargs
        yield SimpleNamespace(
            path=workspace_root,
            base_sha=kwargs["base_sha"],
            head_sha=kwargs["head_sha"],
        )

    def fake_generate_revision_diff(workspace):  # type: ignore[no-untyped-def]
        seen["diff_workspace"] = workspace
        return (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,1 +1,2 @@\n"
            " old\n"
            "+new\n"
        )

    monkeypatch.setattr(github_pr_review, "GitHubApiClient", FakeGitHubClient)
    monkeypatch.setattr(github_pr_review, "AgentOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(github_pr_review, "GitHubPublisher", FakePublisher)
    monkeypatch.setattr(
        github_pr_review,
        "generate_revision_diff",
        fake_generate_revision_diff,
    )
    monkeypatch.setenv("EVENT_LOG_DIR", ".mergewarden/logs")
    monkeypatch.setattr(
        github_pr_review,
        "materialize_github_workspace",
        fake_materialize_github_workspace,
    )

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
    assert seen["workspace_args"] == {
        "owner_repo": "owner/repo",
        "pull_number": 7,
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "token": "installation-token",
    }
    assert seen["diff_workspace"].base_sha == "base-sha"  # type: ignore[union-attr]
    assert Path(seen["review_request"].repo_path) == workspace_root  # type: ignore[union-attr]
    assert seen["review_request"].diff_mode is True  # type: ignore[union-attr]
    assert seen["review_request"].diff_text.startswith("diff --git")  # type: ignore[union-attr]
    assert "EXTERNAL_ONLY = True" in seen["external_read"]["content"]  # type: ignore[index]
    assert seen["publish_request"].head_sha == "head-sha"  # type: ignore[union-attr]
    assert seen["publish_request"].changed_lines == {"src/app.py": [2]}  # type: ignore[union-attr]
    assert (backend_root / ".mergewarden" / "logs" / "run-pr-review.jsonl").exists()
    assert seen["closed"] is True
    assert result.status == "published"
    assert result.check_run_id == 99


def test_pr_review_bridge_cleans_workspace_when_review_raises(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    from src.integrations import github_pr_review

    seen: dict[str, object] = {}
    workspace_paths: list[Path] = []

    class FakeAuthProvider:
        async def get_token(self, installation_id=None):  # type: ignore[no-untyped-def]
            return "installation-token"

    class FakeGitHubClient:
        def __init__(self, token: str) -> None:
            pass

        async def get_pull_diff(self, owner_repo: str, pr_number: int) -> str:
            raise AssertionError("live pull request diff must not be requested")

        async def close(self) -> None:
            seen["closed"] = True

    class FailingOrchestrator:
        async def run_review(self, request):  # type: ignore[no-untyped-def]
            assert Path(request.repo_path).exists()
            raise RuntimeError("review failed")

    @contextmanager
    def fake_materialize_github_workspace(**kwargs):  # type: ignore[no-untyped-def]
        with tempfile.TemporaryDirectory(dir=tmp_path) as raw_path:
            workspace_path = Path(raw_path)
            workspace_paths.append(workspace_path)
            yield SimpleNamespace(
                path=workspace_path,
                base_sha=kwargs["base_sha"],
                head_sha=kwargs["head_sha"],
            )

    monkeypatch.setattr(github_pr_review, "GitHubApiClient", FakeGitHubClient)
    monkeypatch.setattr(github_pr_review, "AgentOrchestrator", FailingOrchestrator)
    monkeypatch.setattr(
        github_pr_review,
        "generate_revision_diff",
        lambda _workspace: "diff --git a/a.py b/a.py\n",
    )
    monkeypatch.setattr(
        github_pr_review,
        "materialize_github_workspace",
        fake_materialize_github_workspace,
    )

    with pytest.raises(RuntimeError, match="review failed"):
        asyncio.run(
            run_github_pull_request_review(
                GitHubPullRequestReviewTrigger(
                    owner_repo="owner/repo",
                    pull_number=7,
                    head_sha="head-sha",
                    base_sha="base-sha",
                    installation_id=123,
                ),
                auth_provider=FakeAuthProvider(),
            )
        )

    assert workspace_paths
    assert not workspace_paths[0].exists()
    assert seen["closed"] is True
