"""Unit tests for the sandboxed command tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.security.sandbox import SandboxResult
from src.tools.base import ToolSafety
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandTimeoutToolError,
    PathNotAllowedError,
)
from src.tools.run_command_tool import RunCommandTool


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

    monkeypatch.setattr(
        "src.tools.run_command_tool.run_sandboxed_command",
        lambda **kwargs: SandboxResult(
            command=kwargs["command"],
            cwd=str(repo_root),
            exit_code=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            duration_ms=12,
        ),
    )

    result = asyncio.run(
        tool.execute(command="echo ok", cwd=str(repo_root), timeout_ms=1000)
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok\n"
    assert result["timed_out"] is False


def test_run_command_tool_raises_on_command_failure(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    monkeypatch.setattr(
        "src.tools.run_command_tool.run_sandboxed_command",
        lambda **kwargs: SandboxResult(
            command=kwargs["command"],
            cwd=str(repo_root),
            exit_code=2,
            stdout="",
            stderr="boom",
            timed_out=False,
            duration_ms=3,
        ),
    )

    with pytest.raises(CommandExecutionToolError):
        asyncio.run(tool.execute(command="bad", cwd=str(repo_root), timeout_ms=1000))


def test_run_command_tool_raises_on_timeout(monkeypatch) -> None:
    tool = RunCommandTool()
    repo_root = Path(__file__).resolve().parent.parent

    monkeypatch.setattr(
        "src.tools.run_command_tool.run_sandboxed_command",
        lambda **kwargs: SandboxResult(
            command=kwargs["command"],
            cwd=str(repo_root),
            exit_code=-1,
            stdout="",
            stderr="",
            timed_out=True,
            duration_ms=1001,
        ),
    )

    with pytest.raises(CommandTimeoutToolError):
        asyncio.run(tool.execute(command="sleep", cwd=str(repo_root), timeout_ms=1))


def test_run_command_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = RunCommandTool()
    allowed_root = Path(__file__).resolve().parent
    outside_dir = Path(__file__).resolve().parent.parent / "src"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(command="echo ok", cwd=str(outside_dir)))
