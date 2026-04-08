"""Smoke tests for the CLI entry point."""

from click.testing import CliRunner

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
