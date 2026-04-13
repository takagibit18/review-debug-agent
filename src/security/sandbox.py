"""Sandbox execution environment.

Wraps subprocess / container calls with timeout, working-directory
constraints, and structured result capture.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from time import perf_counter

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


def run_sandboxed_command(
    *,
    command: str,
    cwd: Path | str,
    timeout_ms: int,
) -> SandboxResult:
    """Run a shell command in a constrained working directory with timeout."""
    resolved_cwd = Path(cwd).resolve()
    start = perf_counter()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(resolved_cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout_ms, 1) / 1000.0,
        )
        return SandboxResult(
            command=command,
            cwd=str(resolved_cwd),
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
            duration_ms=int((perf_counter() - start) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = ""
        stderr = ""
        if isinstance(exc.stdout, str):
            stdout = exc.stdout
        elif isinstance(exc.stdout, bytes):
            stdout = exc.stdout.decode(errors="ignore")
        if isinstance(exc.stderr, str):
            stderr = exc.stderr
        elif isinstance(exc.stderr, bytes):
            stderr = exc.stderr.decode(errors="ignore")
        return SandboxResult(
            command=command,
            cwd=str(resolved_cwd),
            exit_code=-1,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            duration_ms=int((perf_counter() - start) * 1000),
        )
