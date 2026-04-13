"""Read-only directory listing tool for repository exploration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.exceptions import FileNotFoundToolError, FileReadError
from src.tools.path_utils import ensure_path_allowed


class ListDirToolInput(BaseModel):
    """Validated input for directory listing."""

    path: str = Field(default=".", description="Directory path to inspect")
    recursive: bool = Field(
        default=False,
        description="Whether to recurse into subdirectories while listing entries",
    )
    include_hidden: bool = Field(
        default=False,
        description="Whether to include hidden files and directories in the results",
    )
    limit: int = Field(default=100, ge=1, description="Maximum number of entries to return")


class ListDirTool(BaseTool):
    """List files and directories under a target path."""

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="list_dir",
            description=(
                "List files and directories under a target path. Use this when you need to "
                "understand the repository structure before choosing exact file paths or glob "
                "patterns. Prefer this over glob_files when you do not yet know the naming "
                "pattern of the relevant files. This tool only returns directory entries and "
                "basic metadata; it does not read file contents."
            ),
            parameters=ListDirToolInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return directory entries with lightweight metadata."""
        data = ListDirToolInput(**kwargs)
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
            iterator = root.rglob("*") if data.recursive else root.iterdir()
        except (NotADirectoryError, FileNotFoundError) as exc:
            raise FileNotFoundToolError(
                f"Directory not found: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc
        except PermissionError as exc:
            raise FileReadError(
                f"Permission denied while listing directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc

        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            for entry in sorted(iterator, key=lambda item: str(item.resolve())):
                if not data.include_hidden and entry.name.startswith("."):
                    continue
                if len(entries) >= data.limit:
                    truncated = True
                    break
                entry_type = "directory" if entry.is_dir() else "file" if entry.is_file() else "other"
                entries.append(
                    {
                        "path": str(entry.resolve()),
                        "name": entry.name,
                        "type": entry_type,
                    }
                )
        except PermissionError as exc:
            raise FileReadError(
                f"Permission denied while listing directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc
        except OSError as exc:
            raise FileReadError(
                f"Failed to list directory: {root}",
                tool_name=self.spec().name,
                path=str(root),
            ) from exc

        return {
            "root_path": str(root),
            "recursive": data.recursive,
            "include_hidden": data.include_hidden,
            "entries": entries,
            "entry_count": len(entries),
            "truncated": truncated,
        }
