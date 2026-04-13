"""Read-only file tool for the agent tool system."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import FileNotFoundToolError, FileReadError
from src.tools.path_utils import ensure_path_allowed


class FileReadInput(BaseModel):
    """Validated input for reading file content."""

    file_path: str = Field(..., description="Path to the target file")
    offset: int = Field(default=0, ge=0, description="Zero-based start line offset")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read")


class FileReadTool(BaseTool):
    """Read file content with optional line slicing."""

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="read_file",
            description=(
                "Read a text file from disk. Use this when you need the exact file contents "
                "before analysis or editing. Supports optional line slicing through offset and "
                "limit for large files. Do not use this tool to search patterns across many files; "
                "use a search-oriented tool for that."
            ),
            parameters=FileReadInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Read and return file content with metadata."""
        data = FileReadInput(**kwargs)
        path = ensure_path_allowed(Path(data.file_path), tool_name=self.spec().name)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundToolError(
                f"File not found: {path}",
                tool_name=self.spec().name,
                path=str(path),
            ) from exc
        except OSError as exc:
            raise FileReadError(
                f"Failed to read file: {path}",
                tool_name=self.spec().name,
                path=str(path),
            ) from exc
        lines = content.splitlines()
        selected_lines = lines[data.offset :]
        if data.limit is not None:
            selected_lines = selected_lines[: data.limit]
        rendered = "\n".join(
            f"{data.offset + index + 1}: {line}" for index, line in enumerate(selected_lines)
        )
        return {
            "file_path": str(path),
            "content": rendered,
            "start_line": data.offset + 1,
            "line_count": len(selected_lines),
            "truncated": data.limit is not None and (data.offset + len(selected_lines)) < len(lines),
        }
