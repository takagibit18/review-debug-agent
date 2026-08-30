"""Regression coverage for queued revision consistency across the PR pipeline."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import ReviewResponse
from src.integrations import github_pr_review, github_workspace
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    execute_github_pull_request_review,
)


def test_queued_revision_a_stays_pinned_when_repository_head_is_b(
    monkeypatch,
    tmp_path: Path,
) -> None:
    remote, base_sha, revision_a, revision_b = _build_racing_remote(tmp_path)
    assert _git(remote, "rev-parse", "HEAD") == revision_b
    monkeypatch.setattr(github_workspace.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        github_workspace,
        "_repository_url",
        lambda _owner_repo: str(remote),
    )
    observed: dict[str, object] = {}

    class FakeAuthProvider:
        async def get_token(self, installation_id=None):  # type: ignore[no-untyped-def]
            assert installation_id == 77
            return "offline-installation-token"

    class FakeGitHubClient:
        async def list_check_runs(
            self,
            owner_repo: str,
            head_sha: str,
            check_name: str,
        ) -> list[dict[str, object]]:
            observed["listed_check_head"] = head_sha
            return []

        async def update_check_run(
            self,
            owner_repo: str,
            check_run_id: int,
            payload: dict[str, object],
        ) -> dict[str, object]:
            raise AssertionError("there is no existing check in this fixture")

        async def create_check_run(
            self,
            owner_repo: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            observed["check_payload"] = payload
            return {"id": 88, "head_sha": payload["head_sha"]}

        async def close(self) -> None:
            observed["closed"] = True

    class FakeOrchestrator:
        async def run_review(self, request):  # type: ignore[no-untyped-def]
            workspace = Path(request.repo_path)
            observed["workspace_path"] = workspace
            observed["workspace_head"] = _git(workspace, "rev-parse", "HEAD")
            observed["workspace_content"] = (workspace / "pkg" / "service.py").read_text(
                encoding="utf-8"
            )
            observed["agent_diff"] = request.diff_text
            return ReviewResponse(
                run_id="revision-pinned-run",
                report=ReviewReport(summary="Revision A reviewed."),
                context=ContextState(),
            )

    client = FakeGitHubClient()
    monkeypatch.setattr(github_pr_review, "GitHubApiClient", lambda _token: client)
    monkeypatch.setattr(github_pr_review, "AgentOrchestrator", FakeOrchestrator)

    execution = asyncio.run(
        execute_github_pull_request_review(
            GitHubPullRequestReviewTrigger(
                owner_repo="owner/repo",
                pull_number=7,
                base_sha=base_sha,
                head_sha=revision_a,
                installation_id=77,
                action="synchronize",
            ),
            auth_provider=FakeAuthProvider(),
            publish_comments=False,
        )
    )

    expected_diff = _git_from_source(tmp_path / "source", "diff", base_sha, revision_a)
    assert execution.diff_text == expected_diff
    assert observed["agent_diff"] == expected_diff
    assert "MODE = 'A'" in expected_diff
    assert "MODE = 'B'" not in expected_diff
    assert observed["workspace_head"] == revision_a
    assert observed["workspace_content"] == "MODE = 'A'\n"
    assert execution.changed_lines == {"pkg/service.py": [1]}
    assert execution.result.head_sha == revision_a
    assert execution.publish_result.head_sha == revision_a
    assert observed["listed_check_head"] == revision_a
    check_payload = observed["check_payload"]
    assert isinstance(check_payload, dict)
    assert check_payload["head_sha"] == revision_a
    assert check_payload["external_id"] == f"mergewarden:owner/repo:7:{revision_a}"
    assert observed["closed"] is True
    workspace_path = observed["workspace_path"]
    assert isinstance(workspace_path, Path)
    assert not workspace_path.exists()


def _build_racing_remote(tmp_path: Path) -> tuple[Path, str, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Revision Test")
    _git(source, "config", "user.email", "revision@example.test")
    target = source / "pkg" / "service.py"
    target.parent.mkdir(parents=True)
    target.write_text("MODE = 'base'\n", encoding="utf-8")
    _git(source, "add", "pkg/service.py")
    _git(source, "commit", "--quiet", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD")
    target.write_text("MODE = 'A'\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "revision A")
    revision_a = _git(source, "rev-parse", "HEAD")
    target.write_text("MODE = 'B'\n", encoding="utf-8")
    _git(source, "commit", "--quiet", "-am", "revision B")
    revision_b = _git(source, "rev-parse", "HEAD")

    remote = tmp_path / "target.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(remote))
    _git(source, "remote", "add", "target", str(remote))
    _git(source, "push", "--quiet", "target", f"{revision_b}:refs/heads/main")
    _git(source, "push", "--quiet", "target", f"{revision_a}:refs/pull/7/head")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, base_sha, revision_a, revision_b


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _git_from_source(cwd: Path, *args: str) -> str:
    return _git(cwd, *args) + "\n"
