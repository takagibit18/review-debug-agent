"""Pluggable execution backends for sandboxed commands.

The backend interface intentionally takes an ``argv`` list — shell
interpretation is disabled. The default :class:`LocalSubprocessBackend`
runs commands in the current host with an allowlisted environment and
per-stream output truncation. :class:`DockerBackend` is a stub whose
real implementation is deferred to a follow-up PR.
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
            stdout_raw = completed.stdout or ""
            stderr_raw = completed.stderr or ""
            stdout, stdout_truncated = truncate_output(stdout_raw, max_output_bytes)
            stderr, stderr_truncated = truncate_output(stderr_raw, max_output_bytes)
            return SandboxResult(
                command=command_display,
                cwd=str(resolved_cwd),
                exit_code=int(completed.returncode),
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                duration_ms=int((perf_counter() - start) * 1000),
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
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
            stdout, stdout_truncated = truncate_output(stdout_raw, max_output_bytes)
            stderr, stderr_truncated = truncate_output(stderr_raw, max_output_bytes)
            return SandboxResult(
                command=command_display,
                cwd=str(resolved_cwd),
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_ms=int((perf_counter() - start) * 1000),
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )


class DockerBackend:
    """Stub for container-based execution; real impl scheduled for next PR."""

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout_ms: int,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 65536,
    ) -> SandboxResult:
        raise NotImplementedError(
            "Docker backend is not implemented yet; "
            "set EXECUTE_BACKEND=subprocess or follow docs/project_plan.md §3.1."
        )


def get_backend(name: str) -> ExecBackend:
    """Return a backend instance for the given name."""
    lowered = (name or "subprocess").strip().lower()
    if lowered == "docker":
        return DockerBackend()
    return LocalSubprocessBackend()
