"""Pluggable execution backends for sandboxed commands.

The backend interface intentionally takes an ``argv`` list so shell
interpretation stays disabled. The default ``LocalSubprocessBackend``
runs commands on the host with a scrubbed environment and per-stream
output truncation. ``DockerBackend`` runs the validated argv inside a
prebuilt execution image through ``docker run``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Protocol

from src.config import get_settings
from src.security.exec_policy import truncate_output
from src.security.sandbox import SandboxResult
from src.tools.path_utils import get_tool_workspace_root

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
    {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP"}
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


def _truncate_process_output(
    *,
    command_display: str,
    cwd: Path,
    stdout_raw: str,
    stderr_raw: str,
    exit_code: int,
    timed_out: bool,
    start: float,
    max_output_bytes: int,
) -> SandboxResult:
    stdout, stdout_truncated = truncate_output(stdout_raw, max_output_bytes)
    stderr, stderr_truncated = truncate_output(stderr_raw, max_output_bytes)
    return SandboxResult(
        command=command_display,
        cwd=str(cwd),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=int((perf_counter() - start) * 1000),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
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
        command_display = " ".join(argv)
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
            return _truncate_process_output(
                command_display=command_display,
                cwd=resolved_cwd,
                stdout_raw=completed.stdout or "",
                stderr_raw=completed.stderr or "",
                exit_code=int(completed.returncode),
                timed_out=False,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
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
            return _truncate_process_output(
                command_display=command_display,
                cwd=resolved_cwd,
                stdout_raw=stdout_raw,
                stderr_raw=stderr_raw,
                exit_code=-1,
                timed_out=True,
                start=start,
                max_output_bytes=max_output_bytes,
            )


class DockerBackend:
    """Container-based execution backend powered by ``docker run``."""

    def _container_env_args(self, env: dict[str, str] | None) -> list[str]:
        if not env:
            return []
        args: list[str] = []
        for key, value in env.items():
            if key in _CONTAINER_ENV_BLOCKLIST:
                continue
            args.extend(["-e", f"{key}={value}"])
        return args

    @staticmethod
    def _container_cwd(*, workspace_root: Path, cwd: Path, container_root: str) -> str:
        relative = cwd.relative_to(workspace_root)
        if not relative.parts:
            return container_root
        return str(PurePosixPath(container_root, *relative.parts))

    def _build_docker_argv(
        self,
        *,
        argv: list[str],
        workspace_root: Path,
        cwd: Path,
        env: dict[str, str] | None,
    ) -> list[str]:
        settings = get_settings()
        container_root = settings.execute_docker_workdir
        docker_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            settings.execute_docker_network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,source={workspace_root},target={container_root}",
            "-w",
            self._container_cwd(
                workspace_root=workspace_root,
                cwd=cwd,
                container_root=container_root,
            ),
        ]
        if settings.execute_docker_memory_mb > 0:
            docker_argv.extend(["--memory", f"{settings.execute_docker_memory_mb}m"])
        if settings.execute_docker_cpus > 0:
            docker_argv.extend(["--cpus", str(settings.execute_docker_cpus)])
        docker_argv.extend(self._container_env_args(env))
        docker_argv.append(settings.execute_docker_image)
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
        workspace_root = (get_tool_workspace_root() or resolved_cwd).resolve()
        if not resolved_cwd.is_relative_to(workspace_root):
            workspace_root = resolved_cwd
        command_display = " ".join(argv)
        effective_env = env if env is not None else build_scrubbed_env()
        docker_argv = self._build_docker_argv(
            argv=argv,
            workspace_root=workspace_root,
            cwd=resolved_cwd,
            env=effective_env,
        )
        start = perf_counter()
        try:
            completed = subprocess.run(
                docker_argv,
                shell=False,
                cwd=str(workspace_root),
                check=False,
                capture_output=True,
                text=True,
                env=effective_env,
                timeout=max(timeout_ms, 1) / 1000.0,
            )
            return _truncate_process_output(
                command_display=command_display,
                cwd=resolved_cwd,
                stdout_raw=completed.stdout or "",
                stderr_raw=completed.stderr or "",
                exit_code=int(completed.returncode),
                timed_out=False,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
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
            return _truncate_process_output(
                command_display=command_display,
                cwd=resolved_cwd,
                stdout_raw=stdout_raw,
                stderr_raw=stderr_raw,
                exit_code=-1,
                timed_out=True,
                start=start,
                max_output_bytes=max_output_bytes,
            )
        except FileNotFoundError:
            return _truncate_process_output(
                command_display=command_display,
                cwd=resolved_cwd,
                stdout_raw="",
                stderr_raw=(
                    "Docker executable not found on PATH. Install Docker Desktop/Engine "
                    "or set EXECUTE_BACKEND=subprocess."
                ),
                exit_code=127,
                timed_out=False,
                start=start,
                max_output_bytes=max_output_bytes,
            )


def get_backend(name: str) -> ExecBackend:
    """Return a backend instance for the given name."""
    lowered = (name or "subprocess").strip().lower()
    if lowered == "docker":
        return DockerBackend()
    return LocalSubprocessBackend()
