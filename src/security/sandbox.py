"""Sandbox execution entry point.

Dispatches execute-class commands to the configured backend
(subprocess / docker) and returns a structured :class:`SandboxResult`.

The command is expected to be a string; it is parsed/validated by
``src.security.exec_policy.resolve_command`` before dispatch. Shell
interpretation is never used in the backend.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class SandboxResult(BaseModel):
    """Structured result from one sandboxed command run."""

    command: str
    cwd: str
    exit_code: int = Field(default=-1)
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    timed_out: bool = Field(default=False)
    duration_ms: int = Field(default=0, ge=0)
    stdout_truncated: bool = Field(default=False)
    stderr_truncated: bool = Field(default=False)


def run_sandboxed_command(
    *,
    argv: list[str],
    cwd: Path | str,
    timeout_ms: int,
    backend: str | None = None,
    max_output_bytes: int | None = None,
    env: dict[str, str] | None = None,
) -> SandboxResult:
    """Run a validated argv through the configured backend.

    Callers are expected to have already validated ``argv`` via
    :func:`src.security.exec_policy.resolve_command`.
    """
    from src.config import get_settings
    from src.security.backends import build_scrubbed_env, get_backend

    settings = get_settings()
    backend_name = backend if backend is not None else settings.execute_backend
    effective_limit = (
        max_output_bytes
        if max_output_bytes is not None
        else settings.execute_max_output_bytes
    )
    effective_env = env if env is not None else build_scrubbed_env()

    resolved_cwd = Path(cwd).resolve()
    impl = get_backend(backend_name)
    return impl.run(
        argv=list(argv),
        cwd=resolved_cwd,
        timeout_ms=timeout_ms,
        env=effective_env,
        max_output_bytes=effective_limit,
    )
