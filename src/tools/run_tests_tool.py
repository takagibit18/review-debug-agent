"""Convenience execute tool for running a test suite (pytest/unittest)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.config import get_settings
from src.security.exec_policy import (
    resolve_command,
    validate_extra_args,
)
from src.security.sandbox import run_sandboxed_command
from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import (
    CommandExecutionToolError,
    CommandTimeoutToolError,
)
from src.tools.path_utils import ensure_path_allowed


class RunTestsToolInput(BaseModel):
    """Validated input for running a test suite."""

    framework: Literal["pytest", "unittest"] = Field(
        default="pytest", description="Test framework to invoke"
    )
    targets: list[str] = Field(
        default_factory=list,
        description="Test targets (files, node ids, or module paths)",
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Additional pre-split CLI args; shell operators and network flags are rejected.",
    )
    cwd: str = Field(default=".", description="Working directory inside workspace root")
    timeout_ms: int = Field(
        default=60_000,
        ge=1,
        le=600_000,
        description="Maximum execution time in milliseconds",
    )


class RunTestsTool(BaseTool):
    """Run a test suite via the hardened execute backend."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_tests",
            description=(
                "Run the project's test suite via pytest or unittest. Internally builds an argv "
                "that goes through the same allowlist and backend as run_command. Use this for "
                "verification during debug sessions. High-risk: may require user confirmation "
                "and is denied in CI."
            ),
            parameters=RunTestsToolInput.model_json_schema(),
            safety=ToolSafety.EXECUTE,
        )

    def _build_argv(self, data: RunTestsToolInput) -> list[str]:
        safe_targets = validate_extra_args(data.targets, tool_name=self.spec().name)
        safe_extra = validate_extra_args(data.extra_args, tool_name=self.spec().name)
        if data.framework == "pytest":
            return ["pytest", *safe_targets, *safe_extra]
        return ["python", "-m", "unittest", *safe_targets, *safe_extra]

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        data = RunTestsToolInput(**kwargs)
        settings = get_settings()
        allowed_cwd = ensure_path_allowed(Path(data.cwd), tool_name=self.spec().name)
        argv = self._build_argv(data)
        # Re-run through resolve_command using a joined representation only for
        # policy consistency; argv is already well-formed.
        command_string = " ".join(argv)
        resolve_command(
            command_string,
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
                f"Tests timed out after {data.timeout_ms}ms: {command_string}",
                tool_name=self.spec().name,
                path=str(allowed_cwd),
            )
        if result.exit_code != 0:
            stderr = result.stderr.strip()
            message = (
                f"Tests failed with exit code {result.exit_code}: {command_string}"
            )
            if stderr:
                message = f"{message}\n{stderr}"
            raise CommandExecutionToolError(
                message,
                tool_name=self.spec().name,
                path=str(allowed_cwd),
            )
        return result.model_dump()
