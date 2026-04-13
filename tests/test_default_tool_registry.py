"""Tests for the default readonly tool registry."""

from __future__ import annotations

from src.tools import create_default_registry
from src.tools.base import ToolSafety


def test_default_registry_contains_only_readonly_mvp_tools() -> None:
    registry = create_default_registry()

    specs = registry.list_specs()
    names = {spec.name for spec in specs}

    assert names == {"read_file", "glob_files", "grep_files", "list_dir"}
    assert {spec.safety for spec in specs} == {ToolSafety.READONLY}


def test_default_registry_tools_are_concurrency_safe() -> None:
    registry = create_default_registry()

    for tool_name in ("read_file", "glob_files", "grep_files", "list_dir"):
        tool = registry.get(tool_name)
        assert tool is not None
        assert tool.is_concurrency_safe() is True
