"""Tests for eval runner utilities."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import eval.runner as runner_module
from eval.runner import (
    _aggregate_sampled_result,
    _checkout_git_workspace,
    _eval_schema_valid,
    _effective_review_max_iterations,
    _validate_diff_added_lines_against_workspace,
    _prepare_fixture_workspace,
    _is_empty_business_output,
    _match_issues,
    _persist_event_log_to_outputs,
    _read_event_log_stats,
    _semantic_location_matches,
    _validate_expected_locations_against_diff,
    _resolve_event_log_path,
    _resolve_fixture_paths,
    _run_git,
    _sanitize_fixture_id_for_filename,
    load_fixtures,
    run_suite,
)
from eval.schemas import (
    EvalResult,
    Fixture,
    FixtureWorkspace,
    MetricSummary,
    SampledFixtureResult,
)
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def test_run_git_uses_configured_timeout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVAL_GIT_TIMEOUT_SECONDS", "7")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    assert _run_git(["status"], cwd=tmp_path) == "ok"
    assert captured["args"] == [
        "git",
        "-c",
        f"safe.directory={tmp_path.resolve()}",
        "status",
    ]
    assert captured["timeout"] == 7.0


def test_run_git_uses_configured_ssl_backend(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVAL_GIT_SSL_BACKEND", "openssl")
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    assert _run_git(["status"], cwd=tmp_path) == "ok"
    assert captured["args"] == [
        "git",
        "-c",
        "http.sslBackend=openssl",
        "-c",
        f"safe.directory={tmp_path.resolve()}",
        "status",
    ]


def test_offline_cache_uses_existing_mirror_without_remote_update(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = FixtureWorkspace(
        repo_url="https://example.test/acme/repo.git",
        checkout_sha="abc123",
    )
    cache_root = tmp_path / runner_module._workspace_cache_key(workspace.repo_url)
    (cache_root / "objects").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd=None):  # type: ignore[no-untyped-def]
        calls.append(args)
        return ""

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)

    assert runner_module._ensure_git_workspace_cache(
        workspace, tmp_path, offline=True
    ) == cache_root
    assert calls == [["cat-file", "-e", "abc123^{commit}"]]


def test_offline_cache_miss_has_actionable_error(tmp_path: Path) -> None:
    workspace = FixtureWorkspace(
        repo_url="https://example.test/acme/repo.git",
        checkout_sha="abc123",
    )

    try:
        runner_module._ensure_git_workspace_cache(workspace, tmp_path, offline=True)
    except RuntimeError as exc:
        assert "offline cache miss" in str(exc)
        assert workspace.repo_url in str(exc)
    else:
        raise AssertionError("Expected an offline cache miss")


def test_workspace_cache_resumes_valid_partial_mirror(monkeypatch, tmp_path: Path) -> None:
    workspace = FixtureWorkspace(
        repo_url="https://example.test/acme/repo.git",
        checkout_sha="abc123",
    )
    cache_root = tmp_path / runner_module._workspace_cache_key(workspace.repo_url)
    partial_root = cache_root.with_name(f"{cache_root.name}.tmp")
    (partial_root / "objects").mkdir(parents=True)
    (partial_root / "config").write_text("[core]\nrepositoryformatversion = 0\n")
    calls: list[list[str]] = []

    def fake_run_git(args, *, cwd=None):  # type: ignore[no-untyped-def]
        calls.append(args)
        if args[0] == "cat-file":
            raise RuntimeError("missing commit")
        return ""

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)

    assert runner_module._ensure_git_workspace_cache(workspace, tmp_path) == cache_root
    assert not any(args[0] == "clone" for args in calls)
    assert ["fetch", "--quiet", "--depth=1", "origin", "abc123"] in calls


def test_local_smoke_fixture_is_file_backed_and_not_golden() -> None:
    fixture_path = Path("eval/fixtures/local_smoke_pytest_approx_pr8513.json")
    fixture = Fixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))

    assert fixture.metadata.suite == "local_smoke"
    assert fixture.input.workspace is None
    assert set(fixture.input.files) == {
        "src/_pytest/python_api.py",
        "testing/python/approx.py",
    }


def test_run_git_timeout_has_explicit_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVAL_GIT_TIMEOUT_SECONDS", "7")

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    try:
        _run_git(["clone", "repo"], cwd=tmp_path)
    except TimeoutError as exc:
        assert "git clone repo timed out after 7s" in str(exc)
    else:
        raise AssertionError("Expected explicit git timeout error")


def test_run_git_called_process_error_includes_stderr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(
            128,
            args,
            output="",
            stderr="fatal: unable to access repo: SEC_E_NO_CREDENTIALS",
        )

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    try:
        _run_git(["clone", "repo"], cwd=tmp_path)
    except RuntimeError as exc:
        message = str(exc)
        assert "git clone repo failed with exit code 128" in message
        assert "SEC_E_NO_CREDENTIALS" in message
    else:
        raise AssertionError("Expected git failure details")


def _build_source_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "eval@example.com")
    _git(source, "config", "user.name", "Eval Test")
    _git(source, "config", "commit.gpgsign", "false")
    package_dir = source / "pkg"
    package_dir.mkdir()
    (package_dir / "module.py").write_text(
        "from .helper import normalize\n\ndef parse(value):\n    return value\n",
        encoding="utf-8",
    )
    (package_dir / "helper.py").write_text(
        "def normalize(value):\n    return value.strip()\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD")

    (package_dir / "module.py").write_text(
        "from .helper import normalize\n\n"
        "def parse(value):\n"
        "    return normalize(value)\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "head")
    head_sha = _git(source, "rev-parse", "HEAD")
    diff_text = _git(source, "diff", f"{base_sha}..{head_sha}")
    return source, base_sha, head_sha, diff_text


def _build_source_repo_with_hidden_pr_ref(
    tmp_path: Path,
) -> tuple[Path, str, str, str]:
    source, base_sha, head_sha, diff_text = _build_source_repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    _git(remote, "update-ref", "refs/heads/main", base_sha)
    _git(remote, "update-ref", "refs/pull/12/head", head_sha)
    _git(remote, "update-ref", "-d", "refs/heads/master")
    return remote, base_sha, head_sha, diff_text


def test_resolve_event_log_path_returns_existing_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".mergewarden/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    log_dir = repo_root / ".mergewarden" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "run-1.jsonl"
    log_path.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    resolved = _resolve_event_log_path(repo_root, "run-1")
    assert resolved == str(log_path)


def test_resolve_event_log_path_returns_none_when_missing(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".mergewarden/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    resolved = _resolve_event_log_path(repo_root, "missing")
    assert resolved is None


def test_sanitize_fixture_id_for_filename() -> None:
    assert _sanitize_fixture_id_for_filename("a/b\\c") == "a_b_c"


def test_persist_event_log_to_outputs_copies_and_returns_absolute(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "run-1.jsonl"
    src.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    out = _persist_event_log_to_outputs(src, "golden_fixture", "run-1")
    assert out is not None
    dest = tmp_path / "eval" / "outputs" / "event_logs" / "golden_fixture_run-1.jsonl"
    assert Path(out) == dest.resolve()
    assert dest.read_text(encoding="utf-8") == '{"event_type":"model_call"}\n'


def test_persist_event_log_to_outputs_returns_none_when_missing_src() -> None:
    assert _persist_event_log_to_outputs(None, "f", "r") is None


def test_persist_event_log_to_outputs_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    assert _persist_event_log_to_outputs(tmp_path / "nope.jsonl", "f", "r") is None


def test_read_event_log_stats_counts_phase_end_budget_exhaustion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "run-1.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "phase_end",
                        "phase": "format",
                        "payload": {
                            "budget_state": "hard_capped",
                            "budget_exhausted": True,
                        },
                    }
                ),
                json.dumps(
                    {
                        "event_type": "decision",
                        "phase": "finalize",
                        "payload": {"budget_state": "hard_capped"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    stats = _read_event_log_stats(tmp_path, "run-1")

    assert stats["budget_state"] == "hard_capped"
    assert stats["budget_exhausted"] is True


def test_effective_review_max_iterations_caps_unstable_eval_rounds(
    monkeypatch,
) -> None:
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", raising=False)

    assert _effective_review_max_iterations(3) == 2


def test_effective_review_max_iterations_allows_explicit_eval_cap_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", "3")
    monkeypatch.delenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", raising=False)

    assert _effective_review_max_iterations(3) == 3


def test_effective_review_max_iterations_keeps_context_round_when_settings_are_zero(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", "1")
    monkeypatch.setenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", "0")

    assert _effective_review_max_iterations(1) == 2


def test_run_single_forces_eval_prefetch_and_tool_round(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        async def run_review(self, request):  # type: ignore[no-untyped-def]
            return ReviewResponse(
                run_id="eval-run",
                report=ReviewReport(summary="No issues found."),
                context=ContextState(goal="review"),
            )

    monkeypatch.setattr(runner_module, "AgentOrchestrator", FakeOrchestrator)
    fixture = Fixture.model_validate(
        {
            "id": "eval-prefetch",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": (
                    "diff --git a/module.py b/module.py\n"
                    "--- a/module.py\n"
                    "+++ b/module.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+value = 1\n"
                ),
                "files": {"module.py": "value = 1\n"},
            },
            "expected": {"issues": []},
            "metadata": {"reviewed": True},
        }
    )

    result = asyncio.run(runner_module.run_single(fixture, review_max_iterations=1))

    assert result.schema_valid is True
    assert captured["review_diff_first_changed_files"] is True
    assert captured["review_min_tool_iterations"] >= 1
    assert captured["review_max_iterations"] >= 2


def test_resolve_fixture_paths_prefers_manifest(tmp_path: Path) -> None:
    fixtures_root = tmp_path / "eval" / "fixtures"
    fixtures_root.mkdir(parents=True)
    fixture_path = fixtures_root / "sample.json"
    fixture_path.write_text(
        '{"id":"x","type":"review","source":{"repo_full_name":"a/b","pr_number":1},'
        '"input":{"diff_text":"","files":{}},"expected":{"issues":[]},"metadata":{"reviewed":true}}',
        encoding="utf-8",
    )
    (fixtures_root / "manifest.json").write_text(
        '{"generated_at":"2026-01-01T00:00:00+00:00","entries":['
        '{"fixture_id":"x","fixture_type":"review","repo_full_name":"a/b","pr_number":1,'
        '"suite":"golden","path":"eval/fixtures/sample.json","reviewed":true}]}',
        encoding="utf-8",
    )
    resolved = _resolve_fixture_paths(fixtures_root)
    assert resolved == [fixture_path.resolve()]
    fixtures = load_fixtures(fixtures_root, reviewed_only=True)
    assert len(fixtures) == 1


def test_fixture_input_accepts_git_workspace_metadata() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": "",
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": "https://github.com/example/repo.git",
                    "base_sha": "base",
                    "head_sha": "head",
                    "checkout_sha": "head",
                    "diff_base_sha": "base",
                },
            },
            "expected": {"issues": []},
        }
    )

    assert fixture.input.workspace is not None
    assert fixture.input.workspace.kind == "git"
    assert fixture.input.workspace.checkout_sha == "head"


def test_prepare_fixture_workspace_restores_git_snapshot(tmp_path: Path) -> None:
    source, base_sha, head_sha, diff_text = _build_source_repo(tmp_path)
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": diff_text,
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": str(source),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "checkout_sha": head_sha,
                    "diff_base_sha": base_sha,
                },
            },
            "expected": {"issues": []},
        }
    )

    workspace = _prepare_fixture_workspace(fixture, tmp_path / "workspace")

    assert _git(workspace, "rev-parse", "HEAD") == head_sha
    assert "return normalize(value)" in (workspace / "pkg" / "module.py").read_text(
        encoding="utf-8"
    )
    assert (workspace / "pkg" / "helper.py").exists()


def test_prepare_fixture_workspace_fetches_pr_ref_when_checkout_is_not_cloned(
    tmp_path: Path,
) -> None:
    remote, base_sha, head_sha, diff_text = _build_source_repo_with_hidden_pr_ref(
        tmp_path
    )
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 12},
            "input": {
                "diff_text": diff_text,
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": str(remote),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "checkout_sha": head_sha,
                    "diff_base_sha": base_sha,
                },
            },
            "expected": {"issues": []},
        }
    )

    workspace = _prepare_fixture_workspace(fixture, tmp_path / "workspace")

    assert _git(workspace, "rev-parse", "HEAD") == head_sha


def test_prepare_fixture_workspace_fetches_pr_ref_after_checkout_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 12},
            "input": {
                "diff_text": "",
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": "https://github.com/example/repo.git",
                    "base_sha": "base",
                    "head_sha": "head",
                    "checkout_sha": "head",
                    "diff_base_sha": "base",
                },
            },
            "expected": {"issues": []},
        }
    )
    calls: list[list[str]] = []
    checkout_attempts = 0

    def fake_run_git(args: list[str], *, cwd: Path | None = None) -> str:
        nonlocal checkout_attempts
        calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
            return ""
        if args[:2] == ["checkout", "--quiet"]:
            checkout_attempts += 1
            if checkout_attempts == 1:
                raise subprocess.CalledProcessError(1, ["git", *args])
            return ""
        if args[0] == "fetch":
            return ""
        if args == ["rev-parse", "HEAD"]:
            return "head"
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)

    _prepare_fixture_workspace(fixture, tmp_path / "workspace")

    assert [
        "fetch",
        "--quiet",
        "--filter=blob:none",
        "--depth=1",
        "origin",
        "refs/pull/12/head",
    ] in calls
    assert checkout_attempts == 2


def test_checkout_git_workspace_uses_shallow_partial_clone_without_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 12},
            "input": {
                "diff_text": "",
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": "https://github.com/example/repo.git",
                    "checkout_sha": "head",
                },
            },
            "expected": {"issues": []},
        }
    ).input.workspace
    assert workspace is not None
    calls: list[list[str]] = []

    def fake_run_git(args: list[str], *, cwd: Path | None = None) -> str:
        calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
            return ""
        if args[:2] == ["checkout", "--quiet"]:
            return ""
        if args == ["rev-parse", "HEAD"]:
            return "head"
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)

    _checkout_git_workspace(workspace, tmp_path / "workspace", pr_number=12)

    assert calls[0] == [
        "clone",
        "--quiet",
        "--filter=blob:none",
        "--depth=1",
        "https://github.com/example/repo.git",
        str(tmp_path / "workspace"),
    ]


def test_checkout_git_workspace_configures_cached_partial_clone(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = FixtureWorkspace(
        repo_url="https://github.com/example/repo.git",
        checkout_sha="head",
    )
    cache_root = tmp_path / "cache.git"
    calls: list[list[str]] = []

    monkeypatch.setattr(
        runner_module,
        "_ensure_git_workspace_cache",
        lambda *args, **kwargs: cache_root,
    )

    def fake_run_git(args: list[str], *, cwd: Path | None = None) -> str:
        calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
            return ""
        if args[:2] == ["checkout", "--quiet"]:
            return ""
        if args == ["rev-parse", "HEAD"]:
            return "head"
        if args[0] in {"remote", "config"}:
            return ""
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(runner_module, "_run_git", fake_run_git)

    _checkout_git_workspace(
        workspace,
        tmp_path / "workspace",
        workspace_cache_dir=tmp_path / "cache-root",
    )

    assert ["remote", "set-url", "origin", workspace.repo_url] in calls
    assert ["config", "remote.origin.promisor", "true"] in calls
    assert ["config", "remote.origin.partialclonefilter", "blob:none"] in calls
    assert ["config", "extensions.partialClone", "origin"] in calls


def test_prepare_fixture_workspace_reuses_git_workspace_cache(tmp_path: Path) -> None:
    source, base_sha, head_sha, diff_text = _build_source_repo(tmp_path)
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": diff_text,
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": str(source),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "checkout_sha": head_sha,
                    "diff_base_sha": base_sha,
                },
            },
            "expected": {"issues": []},
        }
    )
    cache_dir = tmp_path / "cache"

    first = _prepare_fixture_workspace(
        fixture,
        tmp_path / "workspace-1",
        workspace_cache_dir=cache_dir,
    )
    second = _prepare_fixture_workspace(
        fixture,
        tmp_path / "workspace-2",
        workspace_cache_dir=cache_dir,
    )

    assert _git(first, "rev-parse", "HEAD") == head_sha
    assert _git(second, "rev-parse", "HEAD") == head_sha
    assert len(list(cache_dir.iterdir())) == 1


def test_run_suite_limits_fixture_concurrency(monkeypatch) -> None:
    fixtures = [
        Fixture.model_validate(
            {
                "id": f"fixture-{idx}",
                "type": "review",
                "source": {"repo_full_name": "a/b", "pr_number": idx + 1},
                "input": {"diff_text": "", "files": {}},
                "expected": {"issues": []},
            }
        )
        for idx in range(4)
    ]
    active = 0
    max_active = 0

    async def fake_run_single_sampled(
        fixture: Fixture,
        *,
        samples: int,
        concurrency: int,
        review_max_iterations: int | None,
        temperature: float,
    ) -> SampledFixtureResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await runner_module.asyncio.sleep(0.01)
        active -= 1
        return SampledFixtureResult(
            fixture_id=fixture.id,
            fixture_type=fixture.type,
            expected_count=0,
            runs=[],
        )

    monkeypatch.setattr(
        runner_module, "run_single_sampled", fake_run_single_sampled
    )

    results = runner_module.asyncio.run(
        run_suite(
            fixtures,
            fixture_concurrency=2,
            review_max_iterations=1,
        )
    )

    assert [item.fixture_id for item in results] == [
        "fixture-0",
        "fixture-1",
        "fixture-2",
        "fixture-3",
    ]
    assert max_active == 2


def test_validate_expected_locations_against_diff_requires_changed_workspace_line(
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha, diff_text = _build_source_repo(tmp_path)
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": diff_text,
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": str(source),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "checkout_sha": head_sha,
                    "diff_base_sha": base_sha,
                },
            },
            "expected": {
                "issues": [
                    {
                        "path": "pkg/module.py",
                        "line": 4,
                        "end_line": 4,
                        "description": "normalize hides invalid whitespace.",
                    }
                ]
            },
        }
    )
    workspace = _prepare_fixture_workspace(fixture, tmp_path / "workspace")

    assert _validate_expected_locations_against_diff(fixture, workspace) == []

    bad_fixture = fixture.model_copy(
        update={
            "expected": fixture.expected.model_copy(
                update={
                    "issues": [
                        fixture.expected.issues[0].model_copy(update={"line": 99})
                    ]
                }
            )
        }
    )
    errors = _validate_expected_locations_against_diff(bad_fixture, workspace)

    assert errors
    assert "pkg/module.py:99" in errors[0]


def test_validate_diff_added_lines_against_workspace_detects_stale_diff(
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha, diff_text = _build_source_repo(tmp_path)
    stale_diff = diff_text.replace(
        "+    return normalize(value)",
        "+    return normalize(value.strip())",
    )
    fixture = Fixture.model_validate(
        {
            "id": "workspace-fixture",
            "type": "review",
            "source": {"repo_full_name": "example/repo", "pr_number": 1},
            "input": {
                "diff_text": stale_diff,
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": str(source),
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "checkout_sha": head_sha,
                    "diff_base_sha": base_sha,
                },
            },
            "expected": {"issues": []},
        }
    )
    workspace = _prepare_fixture_workspace(fixture, tmp_path / "workspace")

    errors = _validate_diff_added_lines_against_workspace(fixture, workspace)

    assert errors
    assert "pkg/module.py:4" in errors[0]
    assert "diff added line does not match workspace" in errors[0]


def test_semantic_location_matches_with_overlap() -> None:
    class _Expected:
        path = "src/main.py"
        line = 10
        end_line = 12
        location_pattern = ""

    assert _semantic_location_matches(_Expected(), "src/main.py:11-13")


def test_sampled_metrics_exclude_empty_fixtures_from_hit_rate() -> None:
    positive = SampledFixtureResult(
        fixture_id="positive",
        fixture_type="review",
        expected_count=1,
        runs=[
            EvalResult(fixture_id="positive", fixture_type="review", expected_count=1)
        ],
        pass_at_k_hit_rate=0.0,
        mean_hit_rate=0.0,
        schema_valid_rate=1.0,
    )
    empty = SampledFixtureResult(
        fixture_id="empty",
        fixture_type="review",
        expected_count=0,
        runs=[
            EvalResult(
                fixture_id="empty",
                fixture_type="review",
                actual_count=1,
                false_positive_count=1,
            )
        ],
        pass_at_k_hit_rate=1.0,
        mean_hit_rate=1.0,
        mean_false_positive_rate=1.0,
        schema_valid_rate=1.0,
    )

    metrics = MetricSummary.from_sampled_results([positive, empty])

    assert metrics.hit_rate == 0.0
    assert metrics.mean_hit_rate == 0.0
    assert metrics.pass_at_k_hit_rate == 0.0
    assert metrics.false_positive_rate == 0.5


def test_aggregate_sampled_result_preserves_pass_at_k_for_positive_fixture() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {"diff_text": "", "files": {}},
            "expected": {"issues": [{"location_pattern": "src/main.py"}]},
        }
    )
    runs = [
        EvalResult(
            fixture_id="fixture",
            fixture_type="review",
            expected_count=1,
            matched_count=0,
        ),
        EvalResult(
            fixture_id="fixture",
            fixture_type="review",
            expected_count=1,
            matched_count=1,
        ),
    ]

    sampled = _aggregate_sampled_result(fixture, runs)

    assert sampled.expected_count == 1
    assert sampled.pass_at_k_hit_rate == 1.0
    assert sampled.mean_hit_rate == 0.5


def test_empty_review_output_is_invalid_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(summary="", issues=[]),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is True


def test_placeholder_review_output_is_not_empty_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="Review pipeline completed with placeholder summary.",
            issues=[],
        ),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is False


def test_placeholder_review_output_is_invalid_for_eval_schema() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="Review pipeline completed with placeholder summary.",
            issues=[],
        ),
        context=ContextState(),
    )

    assert _eval_schema_valid(response) is False


def test_nonempty_no_issue_review_output_is_valid_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(summary="No issues found.", issues=[]),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is False


def test_match_issues_ignores_low_confidence_warning_false_positive() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "zero-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {"diff_text": "", "files": {}},
            "expected": {"issues": []},
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/x.py:1",
                    evidence="+ risky_change()",
                    suggestion="check it",
                    confidence=0.8,
                )
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 0
    assert false_positive_count == 0


def test_match_issues_ignores_info_and_style_for_golden_false_positives() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "zero-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {"diff_text": "", "files": {}},
            "expected": {"issues": []},
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="optional suggestions",
            issues=[
                ReviewIssue(
                    severity=Severity.INFO,
                    location="src/x.py:1",
                    evidence="+ optional_context()",
                    suggestion="Consider documenting this behavior.",
                    confidence=0.7,
                ),
                ReviewIssue(
                    severity=Severity.STYLE,
                    location="src/y.py:1",
                    evidence="+ import sys",
                    suggestion="Consider import grouping.",
                    confidence=0.3,
                ),
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 0
    assert false_positive_count == 0


def test_match_issues_counts_high_confidence_warning_on_changed_line() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "positive-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {
                "diff_text": (
                    "diff --git a/src/main.py b/src/main.py\n"
                    "--- a/src/main.py\n"
                    "+++ b/src/main.py\n"
                    "@@ -8,4 +8,5 @@ def parse(value):\n"
                    "     old_call()\n"
                    "+    risky_change()\n"
                ),
                "files": {},
                "workspace": {
                    "kind": "git",
                    "repo_url": "https://github.com/a/b.git",
                    "checkout_sha": "head",
                },
            },
            "expected": {
                "issues": [
                    {
                        "severity": "warning",
                        "path": "src/main.py",
                        "line": 9,
                        "end_line": 9,
                    }
                ]
            },
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="found regression",
            issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                    location="src/main.py:9",
                    evidence="risky_change() now runs inside parse",
                    suggestion="Restore the original guard.",
                    confidence=0.9,
                )
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 1
    assert false_positive_count == 0


def test_match_issues_ignores_warning_fp_when_fixture_expects_critical() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "critical-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {
                "diff_text": (
                    "diff --git a/src/main.py b/src/main.py\n"
                    "--- a/src/main.py\n"
                    "+++ b/src/main.py\n"
                    "@@ -8,4 +8,5 @@ def parse(value):\n"
                    "     old_call()\n"
                    "+    misleading_log_message()\n"
                ),
                "files": {},
            },
            "expected": {
                "issues": [
                    {
                        "severity": "critical",
                        "location_pattern": "src/main.py",
                        "path": "src/main.py",
                        "line": 20,
                    }
                ]
            },
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="minor issue",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/main.py:9",
                    evidence="+    misleading_log_message()",
                    suggestion="Clarify the log message.",
                    confidence=0.95,
                )
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 0
    assert false_positive_count == 0


def test_match_issues_counts_expected_location_warning_with_eval_relaxed_confidence() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "warning-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {
                "diff_text": (
                    "diff --git a/src/main.py b/src/main.py\n"
                    "--- a/src/main.py\n"
                    "+++ b/src/main.py\n"
                    "@@ -8,4 +8,5 @@ def parse(value):\n"
                    "     old_call()\n"
                    "+    return generic_id()\n"
                ),
                "files": {},
            },
            "expected": {
                "issues": [
                    {
                        "severity": "warning",
                        "location_pattern": "src/main.py",
                        "path": "src/main.py",
                        "line": 9,
                    }
                ]
            },
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="found likely regression",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/main.py:9",
                    evidence="+    return generic_id()",
                    suggestion="Do not silently replace descriptive IDs.",
                    confidence=0.7,
                )
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 1
    assert false_positive_count == 0


def test_match_issues_counts_same_file_evidence_line_for_nethermind_callsite() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "nethermind-fixture",
            "type": "review",
            "source": {"repo_full_name": "NethermindEth/nethermind", "pr_number": 5381},
            "input": {
                "diff_text": (
                    "diff --git a/src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs "
                    "b/src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs\n"
                    "--- a/src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs\n"
                    "+++ b/src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs\n"
                    "@@ -75,6 +77,7 @@ public partial class EngineRpcModule : IEngineRpcModule\n"
                    "             try\n"
                    "             {\n"
                    "+                using IDisposable region = _gcKeeper.TryStartNoGCRegion();\n"
                    "                 return await _newPayloadV1Handler.HandleAsync(executionPayload);\n"
                    "             }\n"
                ),
                "files": {},
            },
            "expected": {
                "issues": [
                    {
                        "severity": "critical",
                        "location_pattern": (
                            r"src/Nethermind/Nethermind\.Merge\.Plugin/"
                            r"EngineRpcModule\.Paris\.cs:80"
                        ),
                        "path": (
                            "src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs"
                        ),
                        "line": 80,
                        "end_line": 80,
                    }
                ]
            },
            "metadata": {"suite": "golden", "reviewed": True},
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="found critical constructor regression",
            issues=[
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location=(
                        "src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:24"
                    ),
                    evidence=(
                        "The diff adds `private readonly GCKeeper _gcKeeper;` but "
                        "the constructor does not assign it. When "
                        "`_gcKeeper.TryStartNoGCRegion()` is called at line 80, "
                        "this will throw NullReferenceException at runtime."
                    ),
                    suggestion="Assign the GCKeeper dependency before calling it.",
                    confidence=0.95,
                )
            ],
        ),
        context=ContextState(),
    )

    _, matched_count, false_positive_count = _match_issues(fixture, response)
    assert matched_count == 1
    assert false_positive_count == 0


def test_match_issues_keeps_valid_issue_when_budget_hard_capped_in_log_stats(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "run-1.jsonl").write_text(
        json.dumps(
            {
                "event_type": "phase_end",
                "phase": "format",
                "payload": {
                    "budget_state": "hard_capped",
                    "budget_exhausted": True,
                },
            }
        ),
        encoding="utf-8",
    )
    fixture = Fixture.model_validate(
        {
            "id": "hard-cap-fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {
                "diff_text": (
                    "diff --git a/src/main.py b/src/main.py\n"
                    "--- a/src/main.py\n"
                    "+++ b/src/main.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    "+    fail_closed()\n"
                ),
                "files": {},
            },
            "expected": {
                "issues": [
                    {
                        "severity": "critical",
                        "location_pattern": "src/main.py",
                        "path": "src/main.py",
                        "line": 1,
                    }
                ]
            },
        }
    )
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="found critical issue",
            issues=[
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location="src/main.py:1",
                    evidence="+    fail_closed()",
                    suggestion="Keep the guard.",
                    confidence=0.95,
                )
            ],
        ),
        context=ContextState(),
    )

    stats = _read_event_log_stats(tmp_path, "run-1")
    _, matched_count, false_positive_count = _match_issues(fixture, response)

    assert stats["budget_state"] == "hard_capped"
    assert stats["budget_exhausted"] is True
    assert matched_count == 1
    assert false_positive_count == 0


def test_golden_fixture_distribution_has_required_buckets() -> None:
    real = load_fixtures(Path("eval") / "fixtures", suite="golden", reviewed_only=True)
    synth = load_fixtures(
        Path("eval") / "fixtures", suite="golden_synth", reviewed_only=True
    )
    manifest = json.loads((Path("eval") / "fixtures" / "manifest.json").read_text())

    assert 14 <= len(real) <= 16
    assert sum(1 for fixture in real if fixture.expected.issues) >= 9
    assert sum(1 for fixture in real if not fixture.expected.issues) >= 4

    tag_sets = [set(fixture.metadata.tags) for fixture in real]
    required_tags = {
        "api-compatibility",
        "authorization",
        "concurrency",
        "config",
        "input-validation",
        "negative-sample",
        "precision",
        "runtime",
        "serialization",
        "should-detect",
    }
    covered_tags = set().union(*tag_sets)
    assert required_tags <= covered_tags

    source_keys = {
        (fixture.source.repo_full_name, fixture.source.pr_number) for fixture in real
    }
    assert len(source_keys) == len(real)
    assert synth == []
    fixture_by_path = {
        entry["path"]: Fixture.model_validate_json(Path(entry["path"]).read_text())
        for entry in manifest["entries"]
    }
    assert {entry["fixture_id"] for entry in manifest["entries"]} == {
        fixture.id for fixture in real
    }
    for entry in manifest["entries"]:
        assert entry["reviewed"] == fixture_by_path[entry["path"]].metadata.reviewed


def test_reviewed_golden_fixtures_use_git_workspaces_without_sparse_files(
    tmp_path: Path,
) -> None:
    fixtures = load_fixtures(Path("eval") / "fixtures", suite="golden", reviewed_only=True)

    assert fixtures
    for fixture in fixtures:
        assert fixture.input.files == {}
        assert fixture.input.workspace is not None
        assert fixture.input.workspace.kind == "git"
        assert fixture.input.workspace.repo_url.startswith("https://github.com/")
        assert fixture.input.workspace.checkout_sha
        assert fixture.input.workspace.diff_base_sha
        for issue in fixture.expected.issues:
            assert issue.path
            assert issue.line is not None
            target = tmp_path / fixture.id / issue.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "\n".join("line" for _ in range(issue.end_line or issue.line)),
                encoding="utf-8",
            )
        assert _validate_expected_locations_against_diff(
            fixture, tmp_path / fixture.id
        ) == []


def test_nethermind_expected_issue_tracks_runtime_risk_as_warning() -> None:
    fixtures = load_fixtures(Path("eval") / "fixtures", suite="golden", reviewed_only=True)
    fixture = next(
        item
        for item in fixtures
        if item.id == "golden_NethermindEth_nethermind_pr5381"
    )

    assert fixture.expected.issues[0].category == "runtime"
    assert fixture.expected.issues[0].severity == Severity.WARNING
