"""Sandboxed command execution tool (high-risk execute class)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.config import get_settings
from src.security.exec_policy import resolve_command
from src.security.sandbox import run_sandboxed_command
from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandTimeoutToolError,
)
from src.tools.path_utils import ensure_path_allowed


class RunCommandToolInput(BaseModel):
    """Validated input for sandboxed command execution."""

    command: str = Field(..., min_length=1, description="Shell-like command string; parsed via shlex into argv (shell=False).")
    cwd: str = Field(default=".", description="Working directory (must stay inside workspace root)")
    timeout_ms: int = Field(
        default=30_000,
        ge=1,
        le=600_000,
        description="Maximum execution time in milliseconds",
    )


class RunCommandTool(BaseTool):
    """Execute shell commands through the security sandbox with allowlist policy."""

    def spec(self) -> ToolSpec:
        settings = get_settings()
        allowed = ", ".join(settings.execute_allowed_commands)
        return ToolSpec(
            name="run_command",
            description=(
                "Run a single validated command in a sandboxed working directory with a timeout. "
                "The command string is parsed via shlex and executed with shell=False — shell "
                f"operators (&&, |, >, `, $()) are rejected. Only these first-token commands are "
                f"allowed: {allowed}. The 'git' command is further restricted to readonly "
                "subcommands (status, diff, log, show, rev-parse). Use for explicit verification "
                "actions (e.g. running tests, linters). High-risk: may require user confirmation "
                "and is denied in CI."
            ),
            parameters=RunCommandToolInput.model_json_schema(),
            safety=ToolSafety.EXECUTE,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        data = RunCommandToolInput(**kwargs)
        settings = get_settings()
        allowed_cwd = ensure_path_allowed(Path(data.cwd), tool_name=self.spec().name)
        argv = resolve_command(
            data.command,
            allowed=settings.execute_allowed_commands,
            tool_name=self.spec().name,
        )
        result = run_sandboxed_command(
            argv=argv,
            cwd=allowed_cwd,
            timeout_ms=data.timeout_ms,
            backend=settings.execute_backend,
            max_output_bytes=settings.execute_max_output_bytes,
        )
        if result.timed_out:
            raise CommandTimeoutToolError(
                f"Command timed out after {data.timeout_ms}ms: {data.command}",
                tool_name=self.spec().name,
                path=str(allowed_cwd),
            )
        if result.exit_code != 0:
            stderr = result.stderr.strip()
            message = f"Command failed with exit code {result.exit_code}: {data.command}"
            if stderr:
                message = f"{message}\n{stderr}"
            raise CommandExecutionToolError(
                message,
                tool_name=self.spec().name,
                path=str(allowed_cwd),
            )
        return result.model_dump()
