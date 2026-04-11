"""Unit tests for the directory listing tool."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath

from src.tools.base import ToolSafety
from src.tools.list_dir_tool import ListDirTool


def test_list_dir_tool_spec_exposes_readonly_schema() -> None:
    tool = ListDirTool()

    spec = tool.spec()

    assert spec.name == "list_dir"
    assert spec.safety == ToolSafety.READONLY
    assert "recursive" in spec.parameters["properties"]
    assert spec.parameters["properties"]["limit"]["default"] == 100


def test_list_dir_tool_lists_known_repo_entries() -> None:
    tool = ListDirTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(tool.execute(path=str(repo_root / "src" / "tools"), limit=20))

    assert result["root_path"] == str((repo_root / "src" / "tools").resolve())
    names = {entry["name"] for entry in result["entries"]}
    assert "file_read.py" in names
    assert "glob_tool.py" in names
    assert result["truncated"] is False


def test_list_dir_tool_respects_recursive_and_limit() -> None:
    tool = ListDirTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(
        tool.execute(path=str(repo_root / "tests"), recursive=True, limit=1)
    )

    assert result["entry_count"] == 1
    assert len(result["entries"]) == 1
    assert result["truncated"] is True
    assert PurePath(result["entries"][0]["path"]).parts[-2] == "tests"
