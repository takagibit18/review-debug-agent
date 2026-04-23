"""Unit tests for the sandboxed command tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.security.sandbox import SandboxResult
from src.tools.base import ToolSafety
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandNotAllowedError,
    CommandTimeoutToolError,
    PathNotAllowedError,
)
from src.tools.run_command_tool import RunCommandTool


def _stub_sandbox(monkeypatch, *, exit_code: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False):
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return SandboxResult(
            command=" ".join(kwargs["argv"]),
            cwd=str(kwargs["cwd"]),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=1,
        )

    monkeypatch.setattr("src.tools.run_command_tool.run_sandboxed_command", _fake)
    return captured


def test_run_command_tool_spec_exposes_execute_schema() -> None:
    tool = RunCommandTool()

    spec = tool.spec()

    assert spec.name == "run_command"
    assert spec.safety == ToolSafety.EXECUTE
    assert "command" in spec.parameters["properties"]
    assert spec.parameters["properties"]["timeout_ms"]["default"] == 30000


def test_run_command_tool_returns_structured_success(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    captured = _stub_sandbox(monkeypatch, exit_code=0, stdout="ok\n")

    result = asyncio.run(
        tool.execute(command="python -V", cwd=str(repo_root), timeout_ms=1000)
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok\n"
    assert result["timed_out"] is False
    assert captured["argv"] == ["python", "-V"]


def test_run_command_tool_passes_argv_not_shell(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    captured = _stub_sandbox(monkeypatch)

    asyncio.run(tool.execute(command="pytest -q tests", cwd=str(repo_root)))

    assert isinstance(captured["argv"], list)
    assert captured["argv"] == ["pytest", "-q", "tests"]


def test_run_command_tool_uses_configured_docker_backend(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    monkeypatch.setenv("EXECUTE_BACKEND", "docker")
    captured = _stub_sandbox(monkeypatch)

    asyncio.run(tool.execute(command="python -V", cwd=str(repo_root)))

    assert captured["backend"] == "docker"


def test_run_command_tool_rejects_non_allowlisted_head() -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    with pytest.raises(CommandNotAllowedError):
        asyncio.run(tool.execute(command="rm -rf /", cwd=str(repo_root)))


def test_run_command_tool_rejects_git_write_subcommand() -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    with pytest.raises(CommandNotAllowedError):
        asyncio.run(tool.execute(command="git push origin main", cwd=str(repo_root)))


def test_run_command_tool_raises_on_command_failure(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    _stub_sandbox(monkeypatch, exit_code=2, stderr="boom")

    with pytest.raises(CommandExecutionToolError):
        asyncio.run(tool.execute(command="pytest tests", cwd=str(repo_root), timeout_ms=1000))


def test_run_command_tool_raises_on_timeout(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    _stub_sandbox(monkeypatch, exit_code=-1, timed_out=True)

    with pytest.raises(CommandTimeoutToolError):
        asyncio.run(tool.execute(command="pytest tests", cwd=str(repo_root), timeout_ms=1))


def test_run_command_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = RunCommandTool()
    allowed_root = Path(__file__).resolve().parent
    outside_dir = Path(__file__).resolve().parent.parent / "src"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(command="pytest tests", cwd=str(outside_dir)))
