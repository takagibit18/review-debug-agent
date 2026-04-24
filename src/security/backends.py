"""Pluggable execution backends for sandboxed commands.

The backend interface intentionally takes an ``argv`` list; shell
interpretation is disabled. The default :class:`LocalSubprocessBackend`
runs commands on the host with a scrubbed environment and per-stream
output truncation. :class:`DockerBackend` runs the same argv inside a
disposable container using the configured image and workdir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Protocol

from src.security.exec_policy import truncate_output
from src.security.sandbox import SandboxResult

_ENV_WHITELIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
    }
)

_SENSITIVE_SUFFIXES: tuple[str, ...] = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
_SENSITIVE_PREFIXES: tuple[str, ...] = ("OPENAI_", "AWS_", "AZURE_", "GITHUB_")
_CONTAINER_ENV_BLOCKLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
    }
)


def build_scrubbed_env(
    parent_env: dict[str, str] | None = None,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized environment for subprocess execution."""
    source = dict(parent_env if parent_env is not None else os.environ)
    scrubbed: dict[str, str] = {}
    for key, value in source.items():
        if key in _ENV_WHITELIST:
            scrubbed[key] = value
            continue
        upper = key.upper()
        if any(upper.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
            continue
        if any(upper.startswith(prefix) for prefix in _SENSITIVE_PREFIXES):
            continue
    if extra:
        scrubbed.update(extra)
    return scrubbed


def _render_command(argv: list[str]) -> str:
    return " ".join(argv)


def _completed_result(
    *,
    command_display: str,
    cwd: Path,
    completed: subprocess.CompletedProcess[str],
    start: float,
    max_output_bytes: int,
) -> SandboxResult:
    stdout_raw = completed.stdout or ""
    stderr_raw = completed.stderr or ""
    stdout, stdout_truncated = truncate_output(stdout_raw, max_output_bytes)
    stderr, stderr_truncated = truncate_output(stderr_raw, max_output_bytes)
    return SandboxResult(
        command=command_display,
        cwd=str(cwd),
        exit_code=int(completed.returncode),
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        duration_ms=int((perf_counter() - start) * 1000),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _timeout_result(
    *,
    command_display: str,
    cwd: Path,
    exc: subprocess.TimeoutExpired,
    start: float,
    max_output_bytes: int,
) -> SandboxResult:
    stdout_raw = ""
    stderr_raw = ""
    if isinstance(exc.stdout, str):
        stdout_raw = exc.stdout
    elif isinstance(exc.stdout, bytes):
        stdout_raw = exc.stdout.decode(errors="ignore")
    if isinstance(exc.stderr, str):
        stderr_raw = exc.stderr
    elif isinstance(exc.stderr, bytes):
        stderr_raw = exc.stderr.decode(errors="ignore")
    stdout, stdout_truncated = truncate_output(stdout_raw, max_output_bytes)
    stderr, stderr_truncated = truncate_output(stderr_raw, max_output_bytes)
    return SandboxResult(
        command=command_display,
        cwd=str(cwd),
        exit_code=-1,
        stdout=stdout,
        stderr=stderr,
        timed_out=True,
        duration_ms=int((perf_counter() - start) * 1000),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _os_error_result(
    *,
    command_display: str,
    cwd: Path,
    exc: OSError,
    start: float,
    exit_code: int,
) -> SandboxResult:
    return SandboxResult(
        command=command_display,
        cwd=str(cwd),
        exit_code=exit_code,
        stdout="",
        stderr=str(exc),
        timed_out=False,
        duration_ms=int((perf_counter() - start) * 1000),
        stdout_truncated=False,
        stderr_truncated=False,
    )


class ExecBackend(Protocol):
    """Protocol implemented by all execute-class backends."""

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout_ms: int,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 65536,
    ) -> SandboxResult: ...


class LocalSubprocessBackend:
    """Default backend: invoke argv via ``subprocess.run`` with ``shell=False``."""

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout_ms: int,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 65536,
    ) -> SandboxResult:
        resolved_cwd = Path(cwd).resolve()
        command_display = _render_command(argv)
        effective_env = env if env is not None else build_scrubbed_env()
        start = perf_counter()
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                cwd=str(resolved_cwd),
                check=False,
                capture_output=True,
                text=True,
                env=effective_env,
                timeout=max(timeout_ms, 1) / 1000.0,
            )
            return _completed_result(
                command_display=command_display,
                cwd=resolved_cwd,
                completed=completed,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            return _timeout_result(
                command_display=command_display,
                cwd=resolved_cwd,
                exc=exc,
                start=start,
                max_output_bytes=max_output_bytes,
            )


class DockerBackend:
    """Run argv inside a disposable Docker container."""

    def __init__(
        self,
        *,
        image: str,
        workdir: str,
        network_disabled: bool = True,
        binary: str = "docker",
    ) -> None:
        self.image = image
        self.workdir = workdir
        self.network_disabled = network_disabled
        self.binary = binary

    @staticmethod
    def _mount_source(cwd: Path) -> str:
        resolved = Path(cwd).resolve()
        if os.name == "nt":
            return resolved.as_posix()
        return str(resolved)

    def _container_env(self, env: dict[str, str] | None) -> dict[str, str]:
        filtered: dict[str, str] = {}
        for key, value in (env or {}).items():
            if key.upper() in _CONTAINER_ENV_BLOCKLIST:
                continue
            filtered[key] = value
        filtered.setdefault("PYTHONUNBUFFERED", "1")
        return filtered

    def _build_docker_argv(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None,
    ) -> list[str]:
        docker_argv = [self.binary, "run", "--rm"]
        if self.network_disabled:
            docker_argv.extend(["--network", "none"])
        docker_argv.extend(
            [
                "--workdir",
                self.workdir,
                "--mount",
                f"type=bind,src={self._mount_source(cwd)},dst={self.workdir}",
            ]
        )
        for key, value in sorted(self._container_env(env).items()):
            docker_argv.extend(["--env", f"{key}={value}"])
        docker_argv.append(self.image)
        docker_argv.extend(argv)
        return docker_argv

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout_ms: int,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 65536,
    ) -> SandboxResult:
        resolved_cwd = Path(cwd).resolve()
        command_display = f"[docker:{self.image}] {_render_command(argv)}"
        docker_argv = self._build_docker_argv(argv=argv, cwd=resolved_cwd, env=env)
        start = perf_counter()
        try:
            completed = subprocess.run(
                docker_argv,
                shell=False,
                cwd=str(resolved_cwd),
                check=False,
                capture_output=True,
                text=True,
                env=build_scrubbed_env(),
                timeout=max(timeout_ms, 1) / 1000.0,
            )
            return _completed_result(
                command_display=command_display,
                cwd=resolved_cwd,
                completed=completed,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            return _timeout_result(
                command_display=command_display,
                cwd=resolved_cwd,
                exc=exc,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except FileNotFoundError as exc:
            return _os_error_result(
                command_display=command_display,
                cwd=resolved_cwd,
                exc=exc,
                start=start,
                exit_code=127,
            )


def get_backend(name: str) -> ExecBackend:
    """Return a backend instance for the given name."""
    lowered = (name or "subprocess").strip().lower()
    if lowered == "docker":
        from src.config import get_settings

        settings = get_settings()
        return DockerBackend(
            image=settings.execute_docker_image,
            workdir=settings.execute_docker_workdir,
            network_disabled=settings.execute_docker_network_disabled,
        )
    return LocalSubprocessBackend()
