"""Tests for the run_tests convenience execute tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.security.sandbox import SandboxResult
from src.tools.base import ToolSafety
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandNotAllowedError,
)
from src.tools.run_tests_tool import RunTestsTool


def _stub_sandbox(monkeypatch, *, exit_code: int = 0, stdout: str = ""):
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return SandboxResult(
            command=" ".join(kwargs["argv"]),
            cwd=str(kwargs["cwd"]),
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            timed_out=False,
            duration_ms=1,
        )

    monkeypatch.setattr("src.tools.run_tests_tool.run_sandboxed_command", _fake)
    return captured


def test_spec_is_execute_safety() -> None:
    tool = RunTestsTool()
    spec = tool.spec()
    assert spec.name == "run_tests"
    assert spec.safety == ToolSafety.EXECUTE


def test_pytest_branch_builds_expected_argv(monkeypatch) -> None:
    tool = RunTestsTool()
    repo_root = Path(__file__).resolve().parent.parent
    captured = _stub_sandbox(monkeypatch)

    asyncio.run(
        tool.execute(
            framework="pytest",
            targets=["tests/test_x.py"],
            extra_args=["-q"],
            cwd=str(repo_root),
        )
    )

    assert captured["argv"] == ["pytest", "tests/test_x.py", "-q"]


def test_unittest_branch_builds_expected_argv(monkeypatch) -> None:
    tool = RunTestsTool()
    repo_root = Path(__file__).resolve().parent.parent
    captured = _stub_sandbox(monkeypatch)

    asyncio.run(
        tool.execute(
            framework="unittest",
            targets=["mypkg.tests.test_x"],
            cwd=str(repo_root),
        )
    )

    assert captured["argv"] == ["python", "-m", "unittest", "mypkg.tests.test_x"]


def test_run_tests_tool_uses_configured_docker_backend(monkeypatch) -> None:
    tool = RunTestsTool()
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("EXECUTE_BACKEND", "docker")
    captured = _stub_sandbox(monkeypatch)

    asyncio.run(tool.execute(framework="pytest", cwd=str(repo_root)))

    assert captured["backend"] == "docker"


def test_extra_args_rejects_network_flag() -> None:
    tool = RunTestsTool()
    repo_root = Path(__file__).resolve().parent.parent

    with pytest.raises(CommandNotAllowedError):
        asyncio.run(
            tool.execute(
                framework="pytest",
                extra_args=["--network=host"],
                cwd=str(repo_root),
            )
        )


def test_failure_raises_command_execution_error(monkeypatch) -> None:
    tool = RunTestsTool()
    repo_root = Path(__file__).resolve().parent.parent
    _stub_sandbox(monkeypatch, exit_code=1)

    with pytest.raises(CommandExecutionToolError):
        asyncio.run(tool.execute(framework="pytest", cwd=str(repo_root)))
