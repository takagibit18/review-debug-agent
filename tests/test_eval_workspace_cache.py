"""Targeted workspace cache, offline restore, and prefetch tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import eval.runner as runner_module
import eval.workspace_prefetch as prefetch_module
from eval.schemas import FixtureWorkspace


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


def _build_remote_with_two_pr_refs(tmp_path: Path) -> tuple[Path, str, str, str]:
    source = tmp_path / "src"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "eval@example.com")
    _git(source, "config", "user.name", "Eval Test")
    _git(source, "config", "commit.gpgsign", "false")
    target = source / "module.py"
    target.write_text("def parse(value):\n    return value\n", encoding="utf-8")
    _git(source, "add", "module.py")
    _git(source, "commit", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD")
    target.write_text("def parse(value):\n    return value.strip()\n", encoding="utf-8")
    diff_text = _git(source, "diff")
    _git(source, "add", "module.py")
    _git(source, "commit", "-m", "head")
    head_sha = _git(source, "rev-parse", "HEAD")

    remote = tmp_path / "r.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "remote", "add", "target", str(remote))
    _git(source, "push", "target", "HEAD:refs/heads/main")
    _git(source, "push", "target", f"{base_sha}:refs/pull/1/head")
    _git(source, "push", "target", f"{head_sha}:refs/pull/2/head")
    return remote, base_sha, head_sha, diff_text


def test_targeted_fetch_requests_only_checkout_or_pr_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    commit_present = False

    def fake_run_git(args: list[str], **_kwargs: object) -> str:
        nonlocal commit_present
        calls.append(args)
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/repo.git"
        if args[0] == "cat-file" and not commit_present:
            raise RuntimeError("missing")
        if args[0] == "fetch":
            commit_present = True
        return ""

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)
    runner_module._fetch_cache_ref(tmp_path, "a" * 40, pr_number=7)

    fetches = [call for call in calls if call[0] == "fetch"]
    assert fetches[0][-1] == "a" * 40
    assert fetches[1][-1] == "a" * 40
    assert "--refetch" in fetches[1]
    assert not any("refs/heads/*" in arg for call in fetches for arg in call)
    assert not any("refs/pull/7/head" in call for call in fetches)


def test_targeted_fetch_falls_back_to_pr_head_when_sha_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    commit_present = False

    def fake_run_git(args: list[str], **_kwargs: object) -> str:
        nonlocal commit_present
        calls.append(args)
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/repo.git"
        if args[0] == "cat-file" and not commit_present:
            raise RuntimeError("missing")
        if args[0] == "fetch" and args[-1] == "b" * 40:
            raise RuntimeError("server rejects unadvertised object id")
        if args[0] == "fetch" and args[-1] == "refs/pull/7/head":
            commit_present = True
        return ""

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)
    runner_module._fetch_cache_ref(tmp_path, "b" * 40, pr_number=7)

    fetches = [call for call in calls if call[0] == "fetch"]
    assert [call[-1] for call in fetches] == [
        "b" * 40,
        "refs/pull/7/head",
        "refs/pull/7/head",
    ]
    assert "--refetch" in fetches[-1]


def test_cache_supplements_commits_and_restores_offline_three_times(
    tmp_path: Path,
) -> None:
    remote, base_sha, head_sha, _diff = _build_remote_with_two_pr_refs(tmp_path)
    cache_dir = tmp_path / "cache"
    base = FixtureWorkspace(repo_url=str(remote), checkout_sha=base_sha)
    head = FixtureWorkspace(repo_url=str(remote), checkout_sha=head_sha)

    cache_root = runner_module._ensure_git_workspace_cache(base, cache_dir, pr_number=1)
    assert (
        runner_module._ensure_git_workspace_cache(head, cache_dir, pr_number=2)
        == cache_root
    )

    restored_base = runner_module._checkout_git_workspace(
        base,
        tmp_path / "base",
        pr_number=1,
        workspace_cache_dir=cache_dir,
        offline=True,
    )
    assert _git(restored_base, "rev-parse", "HEAD") == base_sha

    for index in range(3):
        restored = runner_module._checkout_git_workspace(
            head,
            tmp_path / f"head-{index}",
            pr_number=2,
            workspace_cache_dir=cache_dir,
            offline=True,
        )
        assert _git(restored, "rev-parse", "HEAD") == head_sha
        assert (
            (restored / "module.py")
            .read_text(encoding="utf-8")
            .endswith("return value.strip()\n")
        )


def test_second_identical_cache_request_does_not_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, _base_sha, head_sha, _diff = _build_remote_with_two_pr_refs(tmp_path)
    workspace = FixtureWorkspace(repo_url=str(remote), checkout_sha=head_sha)
    cache_dir = tmp_path / "cache"
    runner_module._ensure_git_workspace_cache(workspace, cache_dir, pr_number=2)
    original = runner_module._run_git
    fetches: list[list[str]] = []

    def tracked(args: list[str], **kwargs: object) -> str:
        if args[0] == "fetch":
            fetches.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(runner_module, "_run_git", tracked)
    runner_module._ensure_git_workspace_cache(workspace, cache_dir, pr_number=2)

    assert fetches == []


def test_three_concurrent_workers_publish_one_valid_cache(tmp_path: Path) -> None:
    remote, _base_sha, head_sha, _diff = _build_remote_with_two_pr_refs(tmp_path)
    workspace = FixtureWorkspace(repo_url=str(remote), checkout_sha=head_sha)
    cache_dir = tmp_path / "cache"

    def ensure() -> Path:
        return runner_module._ensure_git_workspace_cache(
            workspace, cache_dir, pr_number=2
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        roots = list(pool.map(lambda _index: ensure(), range(3)))

    assert len(set(roots)) == 1
    assert runner_module._is_valid_bare_cache(roots[0])
    runner_module._verify_cache_snapshot_materialized(roots[0], head_sha)
    assert list(cache_dir.glob("wc-*.tmp")) == []


def test_prefetch_is_idempotent_and_reports_overlay_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, base_sha, _head_sha, diff_text = _build_remote_with_two_pr_refs(tmp_path)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "id": "overlay-prefetch",
                "type": "review",
                "source": {"repo_full_name": "acme/repo", "pr_number": 1},
                "input": {
                    "diff_text": diff_text,
                    "workspace": {
                        "repo_url": str(remote),
                        "checkout_sha": base_sha,
                        "apply_fixture_diff": True,
                    },
                },
                "expected": {"issues": []},
                "metadata": {"suite": "golden", "tags": ["golden"]},
            }
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    first = prefetch_module.prefetch_fixtures([fixture_path], cache_dir=cache_dir)
    expected_snapshot = (
        f"{base_sha}+{hashlib.sha256(diff_text.encode('utf-8')).hexdigest()}"
    )
    assert first["success"] is True
    assert first["records"][0]["repository_snapshot"] == expected_snapshot
    assert first["records"][0]["offline_checkout_verified"] is True
    assert first["records"][0]["cache_size_bytes"] > 0

    original = runner_module._run_git
    fetches: list[list[str]] = []

    def tracked(args: list[str], **kwargs: object) -> str:
        if args[0] == "fetch":
            fetches.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(runner_module, "_run_git", tracked)
    second = prefetch_module.prefetch_fixtures([fixture_path], cache_dir=cache_dir)
    assert second["success"] is True
    assert fetches == []


def test_prefetch_fails_when_commit_exists_but_offline_objects_are_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "id": "incomplete-cache",
                "type": "review",
                "source": {"repo_full_name": "acme/repo", "pr_number": 3},
                "input": {
                    "diff_text": "",
                    "workspace": {
                        "repo_url": "https://github.com/acme/repo.git",
                        "checkout_sha": "a" * 40,
                    },
                },
                "expected": {"issues": []},
                "metadata": {"suite": "golden", "tags": ["golden"]},
            }
        ),
        encoding="utf-8",
    )
    cache = tmp_path / "cache.git"
    cache.mkdir()
    monkeypatch.setattr(
        prefetch_module, "_ensure_git_workspace_cache", lambda *a, **kw: cache
    )
    monkeypatch.setattr(
        prefetch_module,
        "_checkout_git_workspace",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("offline cache is incomplete: missing blob")
        ),
    )

    result = prefetch_module.prefetch_fixtures(
        [fixture_path], cache_dir=tmp_path / "cache"
    )

    assert result["success"] is False
    assert result["failure_count"] == 1
    assert "missing blob" in result["records"][0]["error"]


def test_prefetch_rejects_held_out_fixture_before_cache_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture_path = tmp_path / "held-out.json"
    fixture_path.write_text(
        json.dumps(
            {
                "id": "forbidden",
                "type": "review",
                "source": {"repo_full_name": "acme/repo", "pr_number": 9},
                "input": {
                    "diff_text": "",
                    "workspace": {
                        "repo_url": "https://github.com/acme/repo.git",
                        "checkout_sha": "a" * 40,
                    },
                },
                "expected": {"issues": []},
                "metadata": {"suite": "held_out", "tags": ["held-out"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        prefetch_module,
        "_ensure_git_workspace_cache",
        lambda *a, **kw: pytest.fail("held-out cache access"),
    )

    result = prefetch_module.prefetch_fixtures(
        [fixture_path], cache_dir=tmp_path / "cache"
    )

    assert result["success"] is False
    assert "Held-out fixture is forbidden" in result["records"][0]["error"]
