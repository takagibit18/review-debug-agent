"""Read-only glob search tool for repository file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec


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
        root = Path(data.path).resolve()
        matches = sorted(
            (path.resolve() for path in root.glob(data.pattern)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        selected = matches[: data.limit]
        return {
            "root_path": str(root),
            "pattern": data.pattern,
            "matches": [str(path) for path in selected],
            "match_count": len(selected),
            "truncated": len(matches) > len(selected),
        }
