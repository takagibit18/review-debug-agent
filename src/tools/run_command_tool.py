"""Sandboxed command execution tool (high-risk execute class)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.security.sandbox import run_sandboxed_command
from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandTimeoutToolError,
)
from src.tools.path_utils import ensure_path_allowed


class RunCommandToolInput(BaseModel):
    """Validated input for sandboxed command execution."""

    command: str = Field(..., min_length=1, description="Shell command to execute")
    cwd: str = Field(default=".", description="Working directory for command execution")
    timeout_ms: int = Field(
        default=30_000,
        ge=1,
        le=600_000,
        description="Maximum execution time in milliseconds",
    )


class RunCommandTool(BaseTool):
    """Execute shell commands through the security sandbox."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_command",
            description=(
                "Run a shell command in a sandboxed working directory with timeout limits. "
                "Use this for explicit verification actions such as running tests or build "
                "commands. Do not use this tool for filesystem writes when a safer specialized "
                "tool exists. Important: command execution is high-risk and may require explicit "
                "user confirmation depending on runtime policy."
            ),
            parameters=RunCommandToolInput.model_json_schema(),
            safety=ToolSafety.EXECUTE,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        data = RunCommandToolInput(**kwargs)
        allowed_cwd = ensure_path_allowed(Path(data.cwd), tool_name=self.spec().name)
        result = run_sandboxed_command(
            command=data.command,
            cwd=allowed_cwd,
            timeout_ms=data.timeout_ms,
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
