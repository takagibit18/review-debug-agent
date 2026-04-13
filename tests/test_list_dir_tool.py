"""Unit tests for the directory listing tool."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath

import pytest

from src.tools.base import ToolSafety
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PathNotAllowedError
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


def test_list_dir_tool_truncated_false_when_limit_equals_entry_count(monkeypatch) -> None:
    tool = ListDirTool()
    repo_root = Path(__file__).resolve().parent.parent
    fake_entries = [
        repo_root / "tests" / "test_grep_tool.py",
        repo_root / "tests" / "test_list_dir_tool.py",
    ]

    monkeypatch.setattr(Path, "iterdir", lambda self: iter(fake_entries))

    result = asyncio.run(tool.execute(path=str(repo_root), limit=2))

    assert result["entry_count"] == 2
    assert result["truncated"] is False


def test_list_dir_tool_raises_for_missing_directory() -> None:
    tool = ListDirTool()
    missing_dir = Path(__file__).resolve().parent / "missing-dir"

    with pytest.raises(FileNotFoundToolError):
        asyncio.run(tool.execute(path=str(missing_dir)))


def test_list_dir_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = ListDirTool()
    allowed_root = Path(__file__).resolve().parent
    outside_dir = Path(__file__).resolve().parent.parent / "src"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(path=str(outside_dir)))


def test_list_dir_tool_raises_read_error_for_permission_denied(monkeypatch) -> None:
    tool = ListDirTool()
    repo_root = Path(__file__).resolve().parent.parent

    def _raise_permission(self):  # type: ignore[no-untyped-def]
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", _raise_permission)

    with pytest.raises(FileReadError):
        asyncio.run(tool.execute(path=str(repo_root)))
