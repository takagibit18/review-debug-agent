"""Unit tests for the grep search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath

import pytest

from src.tools.base import ToolSafety
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PathNotAllowedError, PatternError
from src.tools.grep_tool import GrepTool


def test_grep_tool_spec_exposes_readonly_schema() -> None:
    tool = GrepTool()

    spec = tool.spec()

    assert spec.name == "grep_files"
    assert spec.safety == ToolSafety.READONLY
    assert "pattern" in spec.parameters["properties"]
    assert spec.parameters["properties"]["limit"]["default"] == 50


def test_grep_tool_returns_matching_content_from_repo() -> None:
    tool = GrepTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(
        tool.execute(
            pattern="test_file_read_tool_respects_offset_and_limit",
            path=str(repo_root),
            glob="tests/test_file_read_tool.py",
        )
    )

    assert result["root_path"] == str(repo_root)
    assert result["pattern"] == "test_file_read_tool_respects_offset_and_limit"
    assert result["match_count"] == 1
    assert result["matched_file_count"] == 1
    assert PurePath(result["matches"][0]["file_path"]).parts[-2:] == (
        "tests",
        "test_file_read_tool.py",
    )
    assert "test_file_read_tool_respects_offset_and_limit" in result["matches"][0]["line_text"]
    assert result["truncated"] is False


def test_grep_tool_respects_limit() -> None:
    tool = GrepTool()
    repo_root = Path(__file__).resolve().parent.parent

    result = asyncio.run(
        tool.execute(pattern="def test_", path=str(repo_root), glob="tests/test_*.py", limit=1)
    )

    assert result["match_count"] == 1
    assert len(result["matches"]) == 1
    assert result["truncated"] is True


def test_grep_tool_truncated_false_when_total_matches_equals_limit(monkeypatch) -> None:
    tool = GrepTool()
    repo_root = Path(__file__).resolve().parent.parent
    target = Path(__file__).resolve()

    monkeypatch.setattr(Path, "glob", lambda self, pattern: [target])
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "needle\nhaystack\n")

    result = asyncio.run(
        tool.execute(pattern="needle", path=str(repo_root), glob="*.txt", limit=1)
    )

    assert result["match_count"] == 1
    assert result["truncated"] is False


def test_grep_tool_raises_for_missing_directory() -> None:
    tool = GrepTool()
    missing_dir = Path(__file__).resolve().parent / "missing-dir"

    with pytest.raises(FileNotFoundToolError):
        asyncio.run(tool.execute(pattern="needle", path=str(missing_dir)))


def test_grep_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = GrepTool()
    allowed_root = Path(__file__).resolve().parent
    outside_dir = Path(__file__).resolve().parent.parent / "src"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(pattern="needle", path=str(outside_dir)))


def test_grep_tool_raises_pattern_error_for_invalid_regex() -> None:
    tool = GrepTool()
    repo_root = Path(__file__).resolve().parent.parent

    with pytest.raises(PatternError):
        asyncio.run(tool.execute(pattern="[", path=str(repo_root)))


def test_grep_tool_raises_list_error(monkeypatch) -> None:
    tool = GrepTool()
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_list_error(self, pattern):  # type: ignore[no-untyped-def]
        raise OSError("cannot list")

    monkeypatch.setattr(Path, "glob", _raise_list_error)

    with pytest.raises(FileReadError):
        asyncio.run(tool.execute(pattern="needle", path=str(repo_root)))
