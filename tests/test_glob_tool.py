"""Unit tests for the glob search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath

import pytest

from src.tools.base import ToolSafety
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PathNotAllowedError, PatternError
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
    assert PurePath(result["matches"][0]).parts[-2:] == ("tests", "test_file_read_tool.py")
    assert result["truncated"] is False


def test_glob_tool_respects_limit() -> None:
    tool = GlobTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(tool.execute(pattern="tests/test_*.py", path=str(repo_root), limit=1))

    assert result["match_count"] == 1
    assert len(result["matches"]) == 1
    assert result["truncated"] is True


def test_glob_tool_raises_for_missing_directory() -> None:
    tool = GlobTool()
    missing_dir = Path(__file__).resolve().parent / "missing-dir"

    with pytest.raises(FileNotFoundToolError):
        asyncio.run(tool.execute(pattern="*.py", path=str(missing_dir)))


def test_glob_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = GlobTool()
    allowed_root = Path(__file__).resolve().parent
    outside_dir = Path(__file__).resolve().parent.parent / "src"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(pattern="*.py", path=str(outside_dir)))


def test_glob_tool_raises_pattern_error(monkeypatch) -> None:
    tool = GlobTool()
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_pattern_error(self, pattern):  # type: ignore[no-untyped-def]
        raise ValueError("bad pattern")

    monkeypatch.setattr(Path, "glob", _raise_pattern_error)

    with pytest.raises(PatternError):
        asyncio.run(tool.execute(pattern="[", path=str(repo_root)))


def test_glob_tool_raises_list_error(monkeypatch) -> None:
    tool = GlobTool()
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_list_error(self, pattern):  # type: ignore[no-untyped-def]
        raise OSError("cannot list")

    monkeypatch.setattr(Path, "glob", _raise_list_error)

    with pytest.raises(FileReadError):
        asyncio.run(tool.execute(pattern="*.py", path=str(repo_root)))
