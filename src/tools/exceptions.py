"""Tool-layer exceptions with structured metadata."""

from __future__ import annotations


class ToolError(Exception):
    """Base error for all tool failures."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.path = path


class FileNotFoundToolError(ToolError):
    """Target file or directory does not exist."""


class PathNotAllowedError(ToolError):
    """Path violates sandbox or allowed-root constraints."""


class PatternError(ToolError):
    """Regex or glob pattern is invalid."""


class FileReadError(ToolError):
    """I/O failure while reading file content."""


class CommandExecutionToolError(ToolError):
    """Command failed during sandboxed execution."""


class CommandTimeoutToolError(ToolError):
    """Command exceeded execution timeout."""
