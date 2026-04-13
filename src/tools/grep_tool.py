"""Read-only content search tool for repository text files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PatternError
from src.tools.path_utils import ensure_path_allowed


class GrepToolInput(BaseModel):
    """Validated input for regex-based content search."""

    pattern: str = Field(..., description="Regular expression pattern to search for")
    path: str = Field(default=".", description="Root directory used for recursive search")
    glob: str | None = Field(
        default=None,
        description="Optional file glob filter such as '**/*.py' or 'tests/test_*.py'",
    )
    limit: int = Field(default=50, ge=1, description="Maximum number of matches to return")
    case_sensitive: bool = Field(
        default=False,
        description="Whether the regular expression search should be case-sensitive",
    )


class GrepTool(BaseTool):
    """Search text content across files under a repository path."""

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="grep_files",
            description=(
                "Search file contents under a directory using a regular expression pattern. "
                "Use this when you need to find where a symbol, error string, TODO, or code "
                "fragment appears across multiple files. Prefer this over read_file when you do "
                "not yet know which exact file contains the target text. This tool returns "
                "matching file paths and line snippets, not full file contents."
            ),
            parameters=GrepToolInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return regex matches with file and line metadata."""
        data = GrepToolInput(**kwargs)
        root = ensure_path_allowed(Path(data.path), tool_name=self.spec().name)
        flags = 0 if data.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(data.pattern, flags)
        except re.error as exc:
            raise PatternError(
                f"Invalid regex pattern: {data.pattern}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc

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

        pattern = data.glob or "**/*"
        try:
            candidate_paths = sorted(
                (path.resolve() for path in root.glob(pattern) if path.is_file()),
                key=lambda path: str(path),
            )
        except ValueError as exc:
            raise PatternError(
                f"Invalid glob pattern: {pattern}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc
        except OSError as exc:
            raise FileReadError(
                f"Failed to list directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc

        matches: list[dict[str, Any]] = []
        truncated = False
        matched_files: set[str] = set()

        for file_path in candidate_paths:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for line_number, line in enumerate(content.splitlines(), start=1):
                if not regex.search(line):
                    continue
                if len(matches) >= data.limit:
                    truncated = True
                    break
                matched_files.add(str(file_path))
                matches.append(
                    {
                        "file_path": str(file_path),
                        "line_number": line_number,
                        "line_text": line,
                    }
                )
            if truncated:
                break

        return {
            "root_path": str(root),
            "pattern": data.pattern,
            "glob": data.glob,
            "matches": matches,
            "match_count": len(matches),
            "matched_file_count": len(matched_files),
            "truncated": truncated,
        }
