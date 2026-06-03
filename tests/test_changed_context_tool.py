"""Tests for run-scoped changed context tooling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.changed_context_tool import GetChangedContextTool
from src.tools.exceptions import PathNotAllowedError
from src.tools.review_context import ReviewToolContext


def _write_sample_python(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "",
                "class Example:",
                "    enabled = True",
                "    def run(self):",
                "        return 'new'",
                "",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "# filler",
                "def helper():",
                "    return Example()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _sample_diff() -> str:
    return (
        "diff --git a/pkg/sample.py b/pkg/sample.py\n"
        "--- a/pkg/sample.py\n"
        "+++ b/pkg/sample.py\n"
        "@@ -1,7 +1,7 @@\n"
        " import os\n"
        " import sys\n"
        " \n"
        " class Example:\n"
        "+    enabled = True\n"
        "     def run(self):\n"
        "-        return 'old'\n"
        "+        return 'new'\n"
        "@@ -20,2 +20,3 @@\n"
        " # filler\n"
        " def helper():\n"
        "+    return Example()\n"
    )


def test_get_changed_context_returns_hunk_window_imports_and_enclosing_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sample_python(tmp_path / "pkg" / "sample.py")
    context = ReviewToolContext.from_diff(tmp_path, _sample_diff())
    tool = GetChangedContextTool(context)

    result = asyncio.run(tool.execute(file_path="pkg/sample.py", line=7, radius=2))

    assert result["file_path"] == "pkg/sample.py"
    assert result["requested_location"] == {"line": 7, "end_line": None, "hunk_index": None}
    assert result["in_changed_hunk"] is True
    assert result["hunk"]["index"] == 0
    assert result["hunk"]["changed_new_lines"] == [5, 7]
    assert "+        return 'new'" in result["hunk"]["text"]
    assert result["file_window"]["start_line"] == 5
    assert result["file_window"]["end_line"] == 9
    assert "7:         return 'new'" in result["file_window"]["content"]
    assert result["file_window"]["truncated_before"] is True
    assert result["imports_preview"] == "import os\nimport sys"
    assert [item["name"] for item in result["enclosing_symbols"]] == ["Example", "run"]


def test_get_changed_context_selects_hunk_by_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sample_python(tmp_path / "pkg" / "sample.py")
    context = ReviewToolContext.from_diff(tmp_path, _sample_diff())
    tool = GetChangedContextTool(context)

    result = asyncio.run(tool.execute(file_path="pkg/sample.py", hunk_index=1, radius=1))

    assert result["in_changed_hunk"] is True
    assert result["hunk"]["index"] == 1
    assert result["hunk"]["changed_new_lines"] == [22]
    assert "return Example()" in result["file_window"]["content"]


def test_get_changed_context_returns_window_when_line_is_outside_changed_hunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_sample_python(tmp_path / "pkg" / "sample.py")
    context = ReviewToolContext.from_diff(tmp_path, _sample_diff())
    tool = GetChangedContextTool(context)

    result = asyncio.run(tool.execute(file_path="pkg/sample.py", line=3, radius=1))

    assert result["in_changed_hunk"] is False
    assert result["hunk"] is None
    assert result["file_window"]["start_line"] == 2
    assert "3: " in result["file_window"]["content"]


def test_get_changed_context_handles_new_file_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "pkg" / "new_file.py"
    path.parent.mkdir(parents=True)
    path.write_text("def added():\n    return 1\n", encoding="utf-8")
    diff = (
        "diff --git a/pkg/new_file.py b/pkg/new_file.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/pkg/new_file.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def added():\n"
        "+    return 1\n"
    )
    context = ReviewToolContext.from_diff(tmp_path, diff)
    tool = GetChangedContextTool(context)

    result = asyncio.run(tool.execute(file_path="pkg/new_file.py", line=1, radius=1))

    assert result["in_changed_hunk"] is True
    assert result["hunk"]["new_start"] == 1
    assert result["hunk"]["changed_new_lines"] == [1, 2]


def test_get_changed_context_blocks_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    context = ReviewToolContext.from_diff(tmp_path, _sample_diff())
    tool = GetChangedContextTool(context)

    with pytest.raises(PathNotAllowedError):
        asyncio.run(tool.execute(file_path="../outside.py", line=1))
