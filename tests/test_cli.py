"""Smoke tests for the CLI entry point."""

import json
from pathlib import Path

from click.testing import CliRunner

import cli
from cli import main
from src.analyzer.context_state import ContextState, ErrorDetail
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import DebugResponse, ReviewResponse


def test_cli_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "review" in result.output
    assert "debug" in result.output


def test_cli_version(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_review_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["review", "--help"])
    assert result.exit_code == 0
    assert "--diff" in result.output


def test_debug_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["debug", "--help"])
    assert result.exit_code == 0
    assert "--error-log" in result.output


def test_review_command_returns_structured_response(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["review", "."])
    assert result.exit_code == 0
    assert "Running review command..." in result.output
    assert "Run ID:" in result.output
    assert "Summary:" in result.output
    assert "Issues:" in result.output


def test_debug_command_returns_structured_response(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["debug", "."])
    assert result.exit_code == 0
    assert "Running debug command..." in result.output
    assert "Run ID:" in result.output
    assert "Summary:" in result.output
    assert "Steps:" in result.output
    assert "Tracked files:" in result.output


def test_verbose_review_command_includes_json(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["--verbose", "review", "."])
    assert result.exit_code == 0
    assert '"report"' in result.output
    assert '"context"' in result.output
    assert '"triage"' not in result.output
    assert '"has_blocking_findings"' not in result.output


def test_verbose_debug_command_includes_json(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["--verbose", "debug", "."])
    assert result.exit_code == 0
    assert '"summary"' in result.output
    assert '"context"' in result.output


def test_review_command_renders_user_friendly_error(cli_runner: CliRunner, monkeypatch) -> None:
    async def _broken_run_review(self, request):  # type: ignore[no-untyped-def]
        raise RuntimeError("placeholder failure")

    monkeypatch.setattr(cli.AgentOrchestrator, "run_review", _broken_run_review)

    result = cli_runner.invoke(main, ["review", "."])
    assert result.exit_code != 0
    assert "Error: review failed: placeholder failure" in result.output


def test_debug_command_renders_user_friendly_error(cli_runner: CliRunner, monkeypatch) -> None:
    async def _broken_run_debug(self, request):  # type: ignore[no-untyped-def]
        raise RuntimeError("placeholder failure")

    monkeypatch.setattr(cli.AgentOrchestrator, "run_debug", _broken_run_debug)

    result = cli_runner.invoke(main, ["debug", "."])
    assert result.exit_code != 0
    assert "Error: debug failed: placeholder failure" in result.output


def test_review_command_rejects_missing_path(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["review", "missing-path-for-cli-test"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_debug_command_rejects_missing_path(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(main, ["debug", "missing-path-for-cli-test"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_review_command_passes_model_override(cli_runner: CliRunner, monkeypatch) -> None:
    async def _run_review(self, request):  # type: ignore[no-untyped-def]
        assert request.model_name == "gpt-test"
        return ReviewResponse(
            run_id="run-review-model",
            report=ReviewReport(summary="ok"),
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(cli.AgentOrchestrator, "run_review", _run_review)

    result = cli_runner.invoke(main, ["--model", "gpt-test", "review", "."])
    assert result.exit_code == 0
    assert "Run ID: run-review-model" in result.output


def test_advisory_export_writes_payload(cli_runner: CliRunner, tmp_path: Path) -> None:
    response_path = tmp_path / "response.json"
    changed_lines_path = tmp_path / "changed.json"
    output_path = tmp_path / "advisory.json"
    response = ReviewResponse(
        run_id="run-advisory",
        report=ReviewReport(
            summary="summary",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/app.py:10",
                    evidence="+ risky_call()",
                    suggestion="Guard the call.",
                    confidence=0.9,
                )
            ],
        ),
        context=ContextState(),
    )
    response_path.write_text(response.model_dump_json(), encoding="utf-8")
    changed_lines_path.write_text(json.dumps({"src/app.py": [10]}), encoding="utf-8")

    result = cli_runner.invoke(
        main,
        [
            "advisory-export",
            "--response-json",
            str(response_path),
            "--changed-lines-json",
            str(changed_lines_path),
            "--output-json",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-advisory"
    assert payload["inline_comments"][0]["path"] == "src/app.py"
    assert payload["summary_only_issues"] == []


def test_github_advisory_publish_dry_run_outputs_publish_summary(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    response_path = tmp_path / "response.json"
    changed_lines_path = tmp_path / "changed.json"
    output_path = tmp_path / "publish.json"
    response = ReviewResponse(
        run_id="run-publish",
        report=ReviewReport(
            summary="summary",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/app.py:10",
                    evidence="+ risky_call()",
                    suggestion="Guard the call.",
                    confidence=0.9,
                )
            ],
        ),
        context=ContextState(),
    )
    response_path.write_text(response.model_dump_json(), encoding="utf-8")
    changed_lines_path.write_text(json.dumps({"src/app.py": [10]}), encoding="utf-8")

    result = cli_runner.invoke(
        main,
        [
            "github-advisory",
            "publish",
            "--repo",
            "owner/repo",
            "--pr-number",
            "7",
            "--head-sha",
            "abc123",
            "--response-json",
            str(response_path),
            "--changed-lines-json",
            str(changed_lines_path),
            "--dry-run",
            "--output-json",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "dry_run"
    assert payload["owner_repo"] == "owner/repo"
    assert payload["pr_number"] == 7
    assert payload["lifecycle_plan"]["create_count"] == 1
    assert payload["check_run"]["conclusion"] == "neutral"


def test_github_advisory_publish_requires_token_when_not_dry_run(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    response_path = tmp_path / "response.json"
    changed_lines_path = tmp_path / "changed.json"
    response = ReviewResponse(
        run_id="run-publish",
        report=ReviewReport(summary="summary"),
        context=ContextState(),
    )
    response_path.write_text(response.model_dump_json(), encoding="utf-8")
    changed_lines_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("github_token", raising=False)

    result = cli_runner.invoke(
        main,
        [
            "github-advisory",
            "publish",
            "--repo",
            "owner/repo",
            "--pr-number",
            "7",
            "--head-sha",
            "abc123",
            "--response-json",
            str(response_path),
            "--changed-lines-json",
            str(changed_lines_path),
            "--publish",
        ],
    )

    assert result.exit_code != 0
    assert "GITHUB_TOKEN is required" in result.output
    assert "ghp_" not in result.output


def test_github_advisory_publish_closes_client_on_publish_event_loop(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    response_path = tmp_path / "response.json"
    changed_lines_path = tmp_path / "changed.json"
    output_path = tmp_path / "publish.json"
    response = ReviewResponse(
        run_id="run-publish",
        report=ReviewReport(summary="summary"),
        context=ContextState(),
    )
    response_path.write_text(response.model_dump_json(), encoding="utf-8")
    changed_lines_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    class LoopBoundClient:
        def __init__(self, token: str) -> None:
            assert token == "ghp_test"
            self.loop = None

        async def list_review_comments(self, owner_repo: str, pr_number: int):  # type: ignore[no-untyped-def]
            import asyncio

            self.loop = asyncio.get_running_loop()
            return []

        async def create_check_run(self, owner_repo: str, payload):  # type: ignore[no-untyped-def]
            import asyncio

            assert asyncio.get_running_loop() is self.loop
            return {"id": 101}

        async def create_review_comment(self, owner_repo, pr_number, payload):  # type: ignore[no-untyped-def]
            raise AssertionError("no inline comments expected")

        async def update_review_comment(self, owner_repo, comment_id, body):  # type: ignore[no-untyped-def]
            raise AssertionError("no lifecycle comments expected")

        async def close(self) -> None:
            import asyncio

            if asyncio.get_running_loop() is not self.loop:
                raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(cli, "GitHubApiClient", LoopBoundClient)

    result = cli_runner.invoke(
        main,
        [
            "github-advisory",
            "publish",
            "--repo",
            "owner/repo",
            "--pr-number",
            "7",
            "--head-sha",
            "abc123",
            "--response-json",
            str(response_path),
            "--changed-lines-json",
            str(changed_lines_path),
            "--publish",
            "--output-json",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "published"
    assert "github-advisory close failed" not in result.output


def test_review_command_can_export_response_and_run_summary(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    response_path = tmp_path / "review.json"
    summary_path = tmp_path / "summary.json"

    async def _run_review(self, request):  # type: ignore[no-untyped-def]
        return ReviewResponse(
            run_id="run-export",
            report=ReviewReport(summary="summary"),
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(cli.AgentOrchestrator, "run_review", _run_review)

    result = cli_runner.invoke(
        main,
        [
            "review",
            ".",
            "--output-json",
            str(response_path),
            "--summary-json",
            str(summary_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(response_path.read_text(encoding="utf-8"))["run_id"] == "run-export"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == "run-export"
    assert summary["publish_status"] == "not_requested"


def test_review_command_fails_after_export_when_model_auth_fails(
    cli_runner: CliRunner,
    tmp_path: Path,
    monkeypatch,
) -> None:
    response_path = tmp_path / "review.json"
    summary_path = tmp_path / "summary.json"

    async def _run_review(self, request):  # type: ignore[no-untyped-def]
        return ReviewResponse(
            run_id="run-auth-failed",
            report=ReviewReport(summary="Review pipeline completed with placeholder summary."),
            context=ContextState(
                current_files=[request.repo_path],
                errors=[
                    ErrorDetail(
                        file=request.repo_path,
                        message=(
                            "Model analysis failed: Authentication failed for the model "
                            "provider (status=401) [code=auth_failed]"
                        ),
                        category="runtime",
                    )
                ],
            ),
        )

    monkeypatch.setattr(cli.AgentOrchestrator, "run_review", _run_review)

    result = cli_runner.invoke(
        main,
        [
            "review",
            ".",
            "--output-json",
            str(response_path),
            "--summary-json",
            str(summary_path),
        ],
    )

    assert result.exit_code != 0
    assert "review produced no trusted result" in result.output
    assert "auth_failed" in result.output
    assert json.loads(response_path.read_text(encoding="utf-8"))["run_id"] == "run-auth-failed"
    assert json.loads(summary_path.read_text(encoding="utf-8"))["run_id"] == "run-auth-failed"


def test_review_command_renders_triaged_sections(cli_runner: CliRunner, monkeypatch) -> None:
    async def _run_review(self, request):  # type: ignore[no-untyped-def]
        must_fix = ReviewIssue(
            severity=Severity.CRITICAL,
            location="src/auth.py:14",
            evidence="+ if user.is_admin:\n+     return True",
            suggestion="Restore the original authorization check.",
            confidence=0.93,
        )
        warning = ReviewIssue(
            severity=Severity.WARNING,
            location="src/cache.py:8",
            evidence="+ cache.clear() now runs on every request",
            suggestion="Guard cache clearing behind a narrower condition.",
            confidence=0.88,
        )
        info = ReviewIssue(
            severity=Severity.INFO,
            location="src/logging.py:3",
            evidence="+ logger.debug('payload=%s', payload)",
            suggestion="Consider reducing noisy logging in the hot path.",
            confidence=0.70,
        )
        return ReviewResponse(
            run_id="run-review-triage",
            report=ReviewReport(summary="found issues", issues=[must_fix, warning, info]),
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(cli.AgentOrchestrator, "run_review", _run_review)

    result = cli_runner.invoke(main, ["review", "."])
    assert result.exit_code == 0
    assert "Immediate attention: yes" in result.output
    assert "Must-fix critical bugs: 1" in result.output
    assert "Other bug findings: 1" in result.output
    assert "Optimization suggestions: 1" in result.output
    assert "Must-Fix Critical Bugs:" in result.output
    assert "Other Bug Findings:" in result.output
    assert "Optimization Suggestions:" in result.output
    assert "src/auth.py:14" in result.output


def test_debug_command_passes_verbose_flag(cli_runner: CliRunner, monkeypatch) -> None:
    async def _run_debug(self, request):  # type: ignore[no-untyped-def]
        assert request.verbose is True
        return DebugResponse(
            run_id="run-debug-verbose",
            summary="ok",
            hypotheses=[],
            steps=[],
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(cli.AgentOrchestrator, "run_debug", _run_debug)

    result = cli_runner.invoke(main, ["--verbose", "debug", "."])
    assert result.exit_code == 0
    assert "Run ID: run-debug-verbose" in result.output


def test_review_command_passes_permission_mode(cli_runner: CliRunner, monkeypatch) -> None:
    captured: dict[str, object] = {}
    original_init = cli.AgentOrchestrator.__init__

    def _capturing_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured["permission_mode"] = kwargs.get("permission_mode")
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(cli.AgentOrchestrator, "__init__", _capturing_init)

    result = cli_runner.invoke(main, ["--permission-mode", "plan", "review", "."])
    assert result.exit_code == 0
    assert captured["permission_mode"] == "plan"
