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

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _Completed(),
    )

    result = run_sandboxed_command(
        command="echo ok",
        cwd=repo_root,
        timeout_ms=1000,
    )

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.timed_out is False
    assert result.cwd == str(repo_root.resolve())


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
        command="sleep",
        cwd=repo_root,
        timeout_ms=1,
    )

    assert result.exit_code == -1
    assert result.timed_out is True
    assert result.stdout == "partial"
    assert result.stderr == "still running"
