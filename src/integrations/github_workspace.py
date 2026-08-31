"""Materialize one GitHub pull request revision in an isolated workspace."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 300.0
_HEAD_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_REPOSITORY_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_TRACE_ENVIRONMENT_KEYS = {
    "GIT_TRACE",
    "GIT_TRACE2",
    "GIT_TRACE2_EVENT",
    "GIT_TRACE2_PERF",
    "GIT_TRACE_CURL",
    "GIT_TRACE_PACKET",
    "GIT_CURL_VERBOSE",
}


@dataclass(frozen=True)
class GitHubRepositoryWorkspace:
    """An isolated repository checkout pinned to one pull request revision."""

    path: Path
    base_sha: str
    head_sha: str


class GitHubWorkspaceError(RuntimeError):
    """Raised when a GitHub repository workspace cannot be materialized."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@contextmanager
def materialize_github_workspace(
    *,
    owner_repo: str,
    pull_number: int,
    base_sha: str,
    head_sha: str,
    token: str,
) -> Iterator[GitHubRepositoryWorkspace]:
    """Create, populate, and always clean one exact GitHub PR checkout."""
    repository_url = _repository_url(owner_repo)
    normalized_base_sha = base_sha.strip()
    normalized_head_sha = head_sha.strip()
    if pull_number < 1:
        raise GitHubWorkspaceError(
            "workspace_fetch_failed",
            "pull_number must be greater than zero",
        )
    if not _HEAD_SHA_PATTERN.fullmatch(normalized_base_sha):
        raise GitHubWorkspaceError(
            "workspace_checkout_failed",
            "base_sha must be a full hexadecimal Git commit id",
        )
    if not _HEAD_SHA_PATTERN.fullmatch(normalized_head_sha):
        raise GitHubWorkspaceError(
            "workspace_checkout_failed",
            "head_sha must be a full hexadecimal Git commit id",
        )
    if not token:
        raise GitHubWorkspaceError(
            "workspace_fetch_failed",
            "an installation access token is required",
        )

    try:
        temporary = tempfile.TemporaryDirectory(prefix="mergewarden-github-")
    except OSError as exc:
        raise GitHubWorkspaceError(
            "workspace_clone_failed",
            "unable to create an isolated temporary directory",
        ) from exc

    try:
        workspace_path = Path(temporary.name)
        _materialize_repository(
            workspace_path,
            repository_url=repository_url,
            pull_number=pull_number,
            base_sha=normalized_base_sha,
            head_sha=normalized_head_sha,
            token=token,
        )
        yield GitHubRepositoryWorkspace(
            path=workspace_path,
            base_sha=normalized_base_sha,
            head_sha=normalized_head_sha,
        )
    finally:
        temporary.cleanup()


def generate_revision_diff(workspace: GitHubRepositoryWorkspace) -> str:
    """Generate the queued pull request diff from the materialized revisions."""
    result = _run_git(
        [
            "diff",
            "--no-ext-diff",
            "--no-color",
            f"{workspace.base_sha}...{workspace.head_sha}",
            "--",
        ],
        cwd=workspace.path,
        failure_code="workspace_diff_failed",
    )
    _require_success(
        result,
        code="workspace_diff_failed",
        message="unable to generate the queued revision diff",
        secrets=(),
    )
    return result.stdout


def _materialize_repository(
    workspace_path: Path,
    *,
    repository_url: str,
    pull_number: int,
    base_sha: str,
    head_sha: str,
    token: str,
) -> None:
    init_result = _run_git(
        ["init", "--quiet", "."],
        cwd=workspace_path,
        failure_code="workspace_clone_failed",
    )
    _require_success(
        init_result,
        code="workspace_clone_failed",
        message="git init failed",
        secrets=(),
    )
    remote_result = _run_git(
        ["remote", "add", "origin", repository_url],
        cwd=workspace_path,
        failure_code="workspace_clone_failed",
    )
    _require_success(
        remote_result,
        code="workspace_clone_failed",
        message="unable to configure the canonical GitHub remote",
        secrets=(),
    )

    secrets = _credential_secrets(token)
    base_fetch = _run_git(
        ["fetch", "--quiet", "--force", "--no-tags", "origin", base_sha],
        cwd=workspace_path,
        token=token,
        failure_code="workspace_fetch_failed",
    )
    _require_success(
        base_fetch,
        code="workspace_fetch_failed",
        message="unable to fetch the requested base revision",
        secrets=secrets,
    )
    direct_fetch = _run_git(
        ["fetch", "--quiet", "--force", "--no-tags", "origin", head_sha],
        cwd=workspace_path,
        token=token,
        failure_code="workspace_fetch_failed",
    )
    if direct_fetch.returncode != 0:
        pull_ref = f"refs/pull/{pull_number}/head"
        pull_fetch = _run_git(
            [
                "fetch",
                "--quiet",
                "--force",
                "--no-tags",
                "origin",
                f"+{pull_ref}:refs/remotes/origin/mergewarden-pr-head",
            ],
            cwd=workspace_path,
            token=token,
            failure_code="workspace_fetch_failed",
        )
        if pull_fetch.returncode != 0:
            raise GitHubWorkspaceError(
                "workspace_fetch_failed",
                "unable to fetch the requested commit or pull request head; "
                f"direct fetch {_result_details(direct_fetch, secrets)}; "
                f"pull ref fetch {_result_details(pull_fetch, secrets)}",
            )

    base_commit_probe = _run_git(
        ["cat-file", "-e", f"{base_sha}^{{commit}}"],
        cwd=workspace_path,
        failure_code="workspace_fetch_failed",
    )
    _require_success(
        base_commit_probe,
        code="workspace_fetch_failed",
        message="the fetched repository does not contain the requested base_sha",
        secrets=secrets,
    )
    head_commit_probe = _run_git(
        ["cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=workspace_path,
        failure_code="workspace_fetch_failed",
    )
    _require_success(
        head_commit_probe,
        code="workspace_fetch_failed",
        message="the fetched pull request does not contain the requested head_sha",
        secrets=secrets,
    )

    checkout_result = _run_git(
        ["checkout", "--quiet", "--detach", head_sha],
        cwd=workspace_path,
        failure_code="workspace_checkout_failed",
    )
    _require_success(
        checkout_result,
        code="workspace_checkout_failed",
        message="unable to checkout the requested head_sha",
        secrets=secrets,
    )
    resolved_head = _run_git(
        ["rev-parse", "HEAD"],
        cwd=workspace_path,
        failure_code="workspace_checkout_failed",
    )
    _require_success(
        resolved_head,
        code="workspace_checkout_failed",
        message="unable to resolve the checked out revision",
        secrets=secrets,
    )
    actual_sha = resolved_head.stdout.strip()
    if actual_sha.lower() != head_sha.lower():
        raise GitHubWorkspaceError(
            "workspace_revision_mismatch",
            f"expected HEAD {head_sha}, got {actual_sha or '<empty>'}",
        )


def _repository_url(owner_repo: str) -> str:
    parts = owner_repo.strip().split("/")
    if (
        len(parts) != 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(not _REPOSITORY_COMPONENT_PATTERN.fullmatch(part) for part in parts)
    ):
        raise GitHubWorkspaceError(
            "workspace_clone_failed",
            "owner_repo must be a canonical GitHub owner/repository name",
        )
    return f"https://github.com/{parts[0]}/{parts[1]}.git"


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    failure_code: str,
    token: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = _git_environment(token)
    try:
        return subprocess.run(
            ["git", "-c", "core.longpaths=true", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise GitHubWorkspaceError(failure_code, "git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubWorkspaceError(
            failure_code,
            f"git {args[0]} timed out after {_GIT_TIMEOUT_SECONDS:g} seconds",
        ) from exc
    except OSError as exc:
        raise GitHubWorkspaceError(
            failure_code,
            f"unable to start git {args[0]}: {exc.__class__.__name__}",
        ) from exc


def _git_environment(token: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in _TRACE_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    if token is None:
        return environment

    for key in list(environment):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_CONFIG_COUNT"] = "2"
    environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraHeader"
    environment["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {_encoded_credential(token)}"
    environment["GIT_CONFIG_KEY_1"] = "credential.helper"
    environment["GIT_CONFIG_VALUE_1"] = ""
    return environment


def _encoded_credential(token: str) -> str:
    credential = f"x-access-token:{token}".encode("utf-8")
    return base64.b64encode(credential).decode("ascii")


def _credential_secrets(token: str) -> tuple[str, ...]:
    return token, _encoded_credential(token)


def _require_success(
    result: subprocess.CompletedProcess[str],
    *,
    code: str,
    message: str,
    secrets: tuple[str, ...],
) -> None:
    if result.returncode == 0:
        return
    raise GitHubWorkspaceError(
        code,
        f"{message}; {_result_details(result, secrets)}",
    )


def _result_details(
    result: subprocess.CompletedProcess[str],
    secrets: tuple[str, ...],
) -> str:
    output = (result.stderr or result.stdout or "").strip()
    if not output:
        return f"git exited with code {result.returncode}"
    return f"git exited with code {result.returncode}: {_redact(output, secrets)[:1000]}"


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
