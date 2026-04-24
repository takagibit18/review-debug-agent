"""Unit tests for sandbox command execution helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.security.sandbox import run_sandboxed_command
from src.tools.path_utils import tool_workspace_root


def test_run_sandboxed_command_returns_completed_process_output(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    captured: dict = {}

    def _fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run_sandboxed_command(
        argv=["pytest", "-q"],
        cwd=repo_root,
        timeout_ms=1000,
        backend="subprocess",
    )

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.timed_out is False
    assert result.cwd == str(repo_root.resolve())
    assert captured["args"][0] == ["pytest", "-q"]
    assert captured["kwargs"]["shell"] is False
    assert result.stdout_truncated is False


def test_run_sandboxed_command_marks_timeout(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(
            cmd="sleep",
            timeout=0.001,
            output="partial",
            stderr="still running",
        )

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    result = run_sandboxed_command(
        argv=["pytest"],
        cwd=repo_root,
        timeout_ms=1,
        backend="subprocess",
    )

    assert result.exit_code == -1
    assert result.timed_out is True
    assert result.stdout == "partial"
    assert result.stderr == "still running"


def test_run_sandboxed_command_truncates_large_output(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    big = "x" * 5000

    class _Completed:
        returncode = 0
        stdout = big
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())

    result = run_sandboxed_command(
        argv=["pytest"],
        cwd=repo_root,
        timeout_ms=1000,
        backend="subprocess",
        max_output_bytes=1024,
    )

    assert result.stdout_truncated is True
    assert result.stdout.endswith("[truncated]")


def test_run_sandboxed_command_docker_backend_builds_expected_argv(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    nested_cwd = repo_root / "src"

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    captured: dict = {}

    def _fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with tool_workspace_root(repo_root):
        run_sandboxed_command(
            argv=["pytest", "-q", "tests/test_config.py"],
            cwd=nested_cwd,
            timeout_ms=1000,
            backend="docker",
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "CUSTOM_FLAG": "1",
            },
        )

    docker_argv = captured["args"][0]

    assert docker_argv[:2] == ["docker", "run"]
    assert "--rm" in docker_argv
    assert "--network" in docker_argv
    assert "none" in docker_argv
    assert "--cap-drop" in docker_argv
    assert "ALL" in docker_argv
    assert "--security-opt" in docker_argv
    assert "no-new-privileges" in docker_argv
    assert "--mount" in docker_argv
    assert (
        f"type=bind,source={repo_root.resolve()},target=/workspace"
        in docker_argv
    )
    assert docker_argv[docker_argv.index("-w") + 1] == "/workspace/src"
    assert docker_argv[docker_argv.index("-e") + 1] == "LANG=C.UTF-8"
    assert "CUSTOM_FLAG=1" in docker_argv
    assert not any(item.startswith("PATH=") for item in docker_argv)
    assert docker_argv[-4:] == [
        "mergewarden-execute:latest",
        "pytest",
        "-q",
        "tests/test_config.py",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 1.0


def test_run_sandboxed_command_docker_backend_marks_timeout(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(
            cmd=["docker", "run"],
            timeout=0.001,
            output="partial",
            stderr="still running",
        )

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    with tool_workspace_root(repo_root):
        result = run_sandboxed_command(
            argv=["pytest"],
            cwd=repo_root,
            timeout_ms=1,
            backend="docker",
        )

    assert result.exit_code == -1
    assert result.timed_out is True
    assert result.stdout == "partial"
    assert result.stderr == "still running"


def test_run_sandboxed_command_docker_backend_handles_missing_docker_binary(
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_missing_binary(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _raise_missing_binary)

    with tool_workspace_root(repo_root):
        result = run_sandboxed_command(
            argv=["pytest"],
            cwd=repo_root,
            timeout_ms=1000,
            backend="docker",
        )

    assert result.exit_code == 127
    assert result.timed_out is False
    assert "Docker executable not found" in result.stderr
