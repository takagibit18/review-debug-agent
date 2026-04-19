"""Smoke tests for the CLI entry point."""

from click.testing import CliRunner

import cli
from cli import main
from src.analyzer.context_state import ContextState
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
