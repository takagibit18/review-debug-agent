"""Targeted tests for isolated GitHub repository workspaces."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.integrations import github_workspace
from src.integrations.github_workspace import (
    GitHubWorkspaceError,
    materialize_github_workspace,
)

_FAKE_TOKEN = "test-installation-token"


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


def _build_pull_request_remote(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "workspace@example.com")
    _git(source, "config", "user.name", "Workspace Test")
    _git(source, "config", "commit.gpgsign", "false")
    target = source / "external_only.py"
    target.write_text("REVISION = 'A'\n", encoding="utf-8")
    _git(source, "add", "external_only.py")
    _git(source, "commit", "--quiet", "-m", "revision A")
    base_sha = _git(source, "rev-parse", "HEAD")
    target.write_text("REVISION = 'B'\n", encoding="utf-8")
    _git(source, "add", "external_only.py")
    _git(source, "commit", "--quiet", "-m", "revision B")
    head_sha = _git(source, "rev-parse", "HEAD")

    remote = tmp_path / "target.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(remote))
    _git(source, "remote", "add", "target", str(remote))
    _git(source, "push", "--quiet", "target", f"{base_sha}:refs/heads/main")
    _git(source, "push", "--quiet", "target", f"{head_sha}:refs/pull/7/head")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, base_sha, head_sha


def test_materializes_exact_pr_head_without_persisting_token_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote, base_sha, head_sha = _build_pull_request_remote(tmp_path)
    monkeypatch.setattr(github_workspace.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(github_workspace, "_repository_url", lambda _owner_repo: str(remote))
    original_run_git = github_workspace._run_git

    def reject_direct_sha_fetch(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch" and args[-1] == head_sha:
            return subprocess.CompletedProcess(args, 1, "", "direct object fetch rejected")
        return original_run_git(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(github_workspace, "_run_git", reject_direct_sha_fetch)
    workspace_path: Path | None = None

    with materialize_github_workspace(
        owner_repo="owner/repo",
        pull_number=7,
        head_sha=head_sha,
        token=_FAKE_TOKEN,
    ) as workspace:
        workspace_path = workspace.path
        assert _git(workspace.path, "rev-parse", "HEAD") == head_sha
        assert _git(workspace.path, "rev-parse", "HEAD") != base_sha
        assert _git(workspace.path, "branch", "--show-current") == ""
        assert (workspace.path / "external_only.py").read_text(encoding="utf-8") == (
            "REVISION = 'B'\n"
        )
        config_text = (workspace.path / ".git" / "config").read_text(encoding="utf-8")
        assert _FAKE_TOKEN not in config_text
        assert github_workspace._encoded_credential(_FAKE_TOKEN) not in config_text
        assert _git(workspace.path, "remote", "get-url", "origin") == str(remote)

    assert workspace_path is not None
    assert not workspace_path.exists()


def test_workspace_cleanup_runs_when_consumer_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote, _base_sha, head_sha = _build_pull_request_remote(tmp_path)
    monkeypatch.setattr(github_workspace.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(github_workspace, "_repository_url", lambda _owner_repo: str(remote))
    workspace_path: Path | None = None

    with pytest.raises(RuntimeError, match="review exception"):
        with materialize_github_workspace(
            owner_repo="owner/repo",
            pull_number=7,
            head_sha=head_sha,
            token=_FAKE_TOKEN,
        ) as workspace:
            workspace_path = workspace.path
            raise RuntimeError("review exception")

    assert workspace_path is not None
    assert not workspace_path.exists()


def test_workspace_cleanup_runs_when_materialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_path: Path | None = None

    def fail_materialization(path: Path, **_kwargs: object) -> None:
        nonlocal workspace_path
        workspace_path = path
        raise GitHubWorkspaceError("workspace_fetch_failed", "fetch failed")

    monkeypatch.setattr(github_workspace, "_materialize_repository", fail_materialization)

    with pytest.raises(GitHubWorkspaceError, match="workspace_fetch_failed"):
        with materialize_github_workspace(
            owner_repo="owner/repo",
            pull_number=7,
            head_sha="a" * 40,
            token=_FAKE_TOKEN,
        ):
            pass

    assert workspace_path is not None
    assert not workspace_path.exists()


def test_workspace_revision_mismatch_is_explicit_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requested_sha = "a" * 40
    actual_sha = "b" * 40
    workspace_paths: list[Path] = []
    monkeypatch.setattr(github_workspace.tempfile, "tempdir", str(tmp_path))

    def fake_run_git(
        args: list[str],
        *,
        cwd: Path,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        workspace_paths.append(cwd)
        stdout = actual_sha if args[:2] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(github_workspace, "_run_git", fake_run_git)

    with pytest.raises(GitHubWorkspaceError) as captured:
        with materialize_github_workspace(
            owner_repo="owner/repo",
            pull_number=7,
            head_sha=requested_sha,
            token=_FAKE_TOKEN,
        ):
            pass

    assert captured.value.code == "workspace_revision_mismatch"
    assert f"expected HEAD {requested_sha}, got {actual_sha}" in str(captured.value)
    assert workspace_paths
    assert not workspace_paths[0].exists()


def test_workspace_fetch_error_redacts_installation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_token = github_workspace._encoded_credential(_FAKE_TOKEN)
    commands: list[list[str]] = []
    fetch_environments: list[dict[str, str]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "fetch" in command:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            fetch_environments.append(environment)
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                f"authentication failed for {_FAKE_TOKEN} ({encoded_token})",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(github_workspace.subprocess, "run", fake_run)

    with pytest.raises(GitHubWorkspaceError) as captured:
        with materialize_github_workspace(
            owner_repo="owner/repo",
            pull_number=7,
            head_sha="b" * 40,
            token=_FAKE_TOKEN,
        ):
            pass

    message = str(captured.value)
    assert captured.value.code == "workspace_fetch_failed"
    assert _FAKE_TOKEN not in message
    assert encoded_token not in message
    assert "[REDACTED]" in message
    assert fetch_environments
    assert any(
        encoded_token in environment["GIT_CONFIG_VALUE_0"]
        for environment in fetch_environments
    )
    assert all(
        _FAKE_TOKEN not in argument and encoded_token not in argument
        for command in commands
        for argument in command
    )
