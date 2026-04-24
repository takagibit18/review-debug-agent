"""Shared path validation helpers for readonly tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from src.tools.exceptions import PathNotAllowedError

_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar(
    "tool_workspace_root",
    default=None,
)


@contextmanager
def tool_workspace_root(root: Path | str | None):
    """Temporarily bind the allowed workspace root for tool path checks."""
    if root is None:
        token = _WORKSPACE_ROOT.set(None)
    else:
        token = _WORKSPACE_ROOT.set(Path(root).resolve())
    try:
        yield
    finally:
        _WORKSPACE_ROOT.reset(token)


def ensure_path_allowed(path: Path, *, tool_name: str) -> Path:
    """Ensure a tool path stays within the active workspace root."""
    workspace_root = _WORKSPACE_ROOT.get() or Path.cwd().resolve()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (workspace_root / path).resolve()
    if not resolved.is_relative_to(workspace_root):
        raise PathNotAllowedError(
            f"Path is outside the allowed workspace: {resolved}",
            tool_name=tool_name,
            path=str(resolved),
        )
    return resolved


def get_tool_workspace_root() -> Path | None:
    """Return the active workspace root bound for tool execution, if any."""
    return _WORKSPACE_ROOT.get()
