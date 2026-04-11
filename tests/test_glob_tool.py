"""Unit tests for the glob search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.tools.base import ToolSafety
from src.tools.glob_tool import GlobTool


def test_glob_tool_spec_exposes_readonly_schema() -> None:
    tool = GlobTool()

    spec = tool.spec()

    assert spec.name == "glob_files"
    assert spec.safety == ToolSafety.READONLY
    assert "pattern" in spec.parameters["properties"]
    assert spec.parameters["properties"]["limit"]["default"] == 50


def test_glob_tool_returns_matching_paths_from_repo() -> None:
    tool = GlobTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(
        tool.execute(pattern="tests/test_file_read_tool.py", path=str(repo_root))
    )

    assert result["root_path"] == str(repo_root)
    assert result["pattern"] == "tests/test_file_read_tool.py"
    assert result["match_count"] == 1
    assert result["matches"][0].endswith("tests\\test_file_read_tool.py")
    assert result["truncated"] is False


def test_glob_tool_respects_limit() -> None:
    tool = GlobTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(tool.execute(pattern="tests/test_*.py", path=str(repo_root), limit=1))

    assert result["match_count"] == 1
    assert len(result["matches"]) == 1
    assert result["truncated"] is True
