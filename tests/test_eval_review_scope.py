"""Review-scope contracts for Git-backed evaluation fixtures."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.runner import _hydrate_git_review_diff
from eval.schemas import Fixture, FixtureWorkspace


def _workspace_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "repo_url": "https://example.test/repo.git",
        "base_sha": "base",
        "head_sha": "head",
        "checkout_sha": "head",
        "diff_base_sha": "base",
        "review_scope": "full_pr",
    }
    payload.update(updates)
    return payload


def _fixture(
    workspace: FixtureWorkspace, *, diff_text: str = "stale snapshot"
) -> Fixture:
    return Fixture.model_validate(
        {
            "id": "review-scope",
            "type": "review",
            "source": {"repo_full_name": "owner/repo", "pr_number": 1},
            "input": {"diff_text": diff_text, "workspace": workspace.model_dump()},
            "expected": {"issues": []},
            "metadata": {"reviewed": True},
        }
    )


def test_full_pr_scope_requires_head_workspace_without_overlay() -> None:
    workspace = FixtureWorkspace.model_validate(_workspace_payload())

    assert workspace.review_scope == "full_pr"
    assert workspace.review_paths == []

    for invalid in (
        {"checkout_sha": "base"},
        {"diff_base_sha": "different"},
        {"apply_fixture_diff": True},
        {"review_paths": ["src/app.py"]},
    ):
        with pytest.raises(ValidationError):
            FixtureWorkspace.model_validate(_workspace_payload(**invalid))


def test_partial_pr_scope_requires_explicit_safe_paths_and_reason() -> None:
    workspace = FixtureWorkspace.model_validate(
        _workspace_payload(
            review_scope="partial_pr",
            review_paths=["tests/test_app.py", "src\\app.py", "src/app.py"],
            scope_reason="Review only the independently deployable service change.",
        )
    )

    assert workspace.review_paths == ["src/app.py", "tests/test_app.py"]

    for invalid in (
        {"review_scope": "partial_pr", "scope_reason": "documented"},
        {"review_scope": "partial_pr", "review_paths": ["src/app.py"]},
        {
            "review_scope": "partial_pr",
            "review_paths": ["../outside.py"],
            "scope_reason": "documented",
        },
    ):
        with pytest.raises(ValidationError):
            FixtureWorkspace.model_validate(_workspace_payload(**invalid))


def test_full_pr_diff_is_derived_from_git_not_stored_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = FixtureWorkspace.model_validate(_workspace_payload())
    fixture = _fixture(workspace)
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], **_: object) -> str:
        calls.append(args)
        return "diff --git a/src/app.py b/src/app.py\n+complete"

    monkeypatch.setattr("eval.runner._run_git", fake_run_git)

    _hydrate_git_review_diff(fixture, Path("repo"))

    assert fixture.input.diff_text.endswith("+complete\n")
    assert "stale snapshot" not in fixture.input.diff_text
    assert calls == [
        [
            "diff",
            "--no-ext-diff",
            "--find-renames=50%",
            "--binary",
            "base",
            "head",
        ]
    ]


def test_partial_pr_diff_uses_only_declared_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = FixtureWorkspace.model_validate(
        _workspace_payload(
            review_scope="partial_pr",
            review_paths=["tests/test_app.py", "src/app.py"],
            scope_reason="Review only the independently deployable service change.",
        )
    )
    fixture = _fixture(workspace)
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], **_: object) -> str:
        calls.append(args)
        return "diff --git a/src/app.py b/src/app.py\n+scoped"

    monkeypatch.setattr("eval.runner._run_git", fake_run_git)

    _hydrate_git_review_diff(fixture, Path("repo"))

    assert calls[0][-3:] == ["--", "src/app.py", "tests/test_app.py"]


def test_nonlegacy_scope_rejects_empty_git_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(FixtureWorkspace.model_validate(_workspace_payload()))
    monkeypatch.setattr("eval.runner._run_git", lambda *_args, **_kwargs: "")

    with pytest.raises(ValueError, match="resolved an empty full_pr diff"):
        _hydrate_git_review_diff(fixture, Path("repo"))


def test_legacy_scope_preserves_existing_fixture_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = FixtureWorkspace.model_validate(
        {
            "repo_url": "https://example.test/repo.git",
            "checkout_sha": "legacy",
        }
    )
    fixture = _fixture(workspace)

    def unexpected_git(*_: object, **__: object) -> str:
        raise AssertionError("legacy scope must not derive a Git diff")

    monkeypatch.setattr("eval.runner._run_git", unexpected_git)

    _hydrate_git_review_diff(fixture, Path("repo"))

    assert fixture.input.diff_text == "stale snapshot"
