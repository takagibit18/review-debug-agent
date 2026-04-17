"""Unit tests for sandbox command execution helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.security.sandbox import run_sandboxed_command


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


def test_run_sandboxed_command_docker_backend_is_stub() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    import pytest

    with pytest.raises(NotImplementedError):
        run_sandboxed_command(
            argv=["pytest"],
            cwd=repo_root,
            timeout_ms=1000,
            backend="docker",
        )
