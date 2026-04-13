"""Unit tests for the file read tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.base import ToolSafety
from src.tools.exceptions import FileNotFoundToolError, FileReadError, PathNotAllowedError
from src.tools.file_read import FileReadTool


def test_file_read_tool_spec_exposes_readonly_schema() -> None:
    tool = FileReadTool()

    spec = tool.spec()

    assert spec.name == "read_file"
    assert spec.safety == ToolSafety.READONLY
    assert "file_path" in spec.parameters["properties"]
    assert spec.parameters["properties"]["offset"]["default"] == 0


def test_file_read_tool_reads_full_file() -> None:
    file_path = Path(__file__).resolve()
    tool = FileReadTool()

    result = asyncio.run(tool.execute(file_path=str(file_path)))

    assert result["file_path"] == str(file_path)
    assert result["content"].startswith('1: """Unit tests for the file read tool."""')
    assert result["start_line"] == 1
    assert result["line_count"] >= 3
    assert result["truncated"] is False


def test_file_read_tool_respects_offset_and_limit() -> None:
    file_path = Path(__file__).resolve()
    tool = FileReadTool()

    result = asyncio.run(tool.execute(file_path=str(file_path), offset=1, limit=2))

    assert result["content"] == "2: \n3: from __future__ import annotations"
    assert result["start_line"] == 2
    assert result["line_count"] == 2
    assert result["truncated"] is True


def test_file_read_tool_raises_file_not_found() -> None:
    tool = FileReadTool()
    missing_path = Path(__file__).resolve().parent / "missing-file.txt"

    with pytest.raises(FileNotFoundToolError):
        asyncio.run(tool.execute(file_path=str(missing_path)))


def test_file_read_tool_blocks_path_outside_workspace(monkeypatch) -> None:
    tool = FileReadTool()
    allowed_root = Path(__file__).resolve().parent
    outside_path = Path(__file__).resolve().parent.parent / "cli.py"

    monkeypatch.setattr(Path, "cwd", lambda: allowed_root)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(file_path=str(outside_path)))


def test_file_read_tool_raises_read_error(monkeypatch) -> None:
    tool = FileReadTool()
    file_path = Path(__file__).resolve()

    def _raise_read_error(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", _raise_read_error)

    with pytest.raises(FileReadError):
        asyncio.run(tool.execute(file_path=str(file_path)))
