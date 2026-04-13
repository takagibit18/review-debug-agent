"""Read-only glob search tool for repository file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PatternError
from src.tools.path_utils import ensure_path_allowed


class GlobToolInput(BaseModel):
    """Validated input for glob-based file discovery."""

    pattern: str = Field(..., description="Glob pattern such as '**/*.py' or 'tests/test_*.py'")
    path: str = Field(default=".", description="Root directory used for glob expansion")
    limit: int = Field(default=50, ge=1, description="Maximum number of matched paths to return")


class GlobTool(BaseTool):
    """Find file paths under a directory using glob patterns."""

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="glob_files",
            description=(
                "Find file paths that match a glob pattern under a directory. Use this when you "
                "need to discover candidate files before reading them. Prefer this over read_file "
                "when you do not yet know the exact file path. This tool only returns paths and "
                "does not read file contents."
            ),
            parameters=GlobToolInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return matched file paths with deterministic ordering."""
        data = GlobToolInput(**kwargs)
        root = ensure_path_allowed(Path(data.path), tool_name=self.spec().name)
        if not root.exists():
            raise FileNotFoundToolError(
                f"Directory not found: {root}",
                tool_name=self.spec().name,
                path=str(root),
            )
        if not root.is_dir():
            raise FileNotFoundToolError(
                f"Path is not a directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            )

        try:
            matches = sorted(
                (path.resolve() for path in root.glob(data.pattern)),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except ValueError as exc:
            raise PatternError(
                f"Invalid glob pattern: {data.pattern}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc
        except OSError as exc:
            raise FileReadError(
                f"Failed to list directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc
        selected = matches[: data.limit]
        return {
            "root_path": str(root),
            "pattern": data.pattern,
            "matches": [str(path) for path in selected],
            "match_count": len(selected),
            "truncated": len(matches) > len(selected),
        }
