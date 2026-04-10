"""Smoke tests for the CLI entry point."""

from click.testing import CliRunner

import cli
from cli import main


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
