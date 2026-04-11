"""Unit tests for the file read tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.tools.base import ToolSafety
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
