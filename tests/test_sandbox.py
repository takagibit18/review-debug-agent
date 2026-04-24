"""Unit tests for sandbox command execution helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.security.sandbox import run_sandboxed_command


def _docker_mount_source(path: Path) -> str:
    resolved = path.resolve()
    if os.name == "nt":
        return resolved.as_posix()
    return str(resolved)


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


def test_run_sandboxed_command_docker_backend_builds_container_command(
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("EXECUTE_DOCKER_IMAGE", "sandbox-image:latest")
    monkeypatch.setenv("EXECUTE_DOCKER_WORKDIR", "sandbox")
    monkeypatch.setenv("EXECUTE_DOCKER_NETWORK_DISABLED", "true")

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
        backend="docker",
        env={
            "PATH": "C:\\Windows\\System32",
            "LANG": "C.UTF-8",
            "SAFE_VAR": "yes",
        },
    )

    docker_argv = captured["args"][0]
    env_values = [
        docker_argv[index + 1]
        for index, token in enumerate(docker_argv[:-1])
        if token == "--env"
    ]

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.command == "[docker:sandbox-image:latest] pytest -q"
    assert docker_argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in docker_argv
    assert "none" in docker_argv
    assert "--workdir" in docker_argv
    assert docker_argv[docker_argv.index("--workdir") + 1] == "/sandbox"
    assert docker_argv[docker_argv.index("--mount") + 1] == (
        f"type=bind,src={_docker_mount_source(repo_root)},dst=/sandbox"
    )
    assert "LANG=C.UTF-8" in env_values
    assert "SAFE_VAR=yes" in env_values
    assert "PYTHONUNBUFFERED=1" in env_values
    assert all(not value.startswith("PATH=") for value in env_values)
    image_index = docker_argv.index("sandbox-image:latest")
    assert docker_argv[image_index + 1 :] == ["pytest", "-q"]


def test_run_sandboxed_command_docker_backend_returns_structured_error_when_missing(
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("EXECUTE_DOCKER_IMAGE", "sandbox-image:latest")

    def _raise_missing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", _raise_missing)

    result = run_sandboxed_command(
        argv=["pytest"],
        cwd=repo_root,
        timeout_ms=1000,
        backend="docker",
    )

    assert result.exit_code == 127
    assert result.timed_out is False
    assert "docker" in result.stderr.lower()
