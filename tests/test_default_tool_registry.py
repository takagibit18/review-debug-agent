"""Tests for the default readonly tool registry."""

from __future__ import annotations

from src.tools import create_default_registry
from src.tools.base import ToolSafety
from src.tools.review_context import ReviewToolContext


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


def test_default_registry_registers_review_tools_when_context_exists(tmp_path) -> None:
    context = ReviewToolContext.from_diff(
        tmp_path,
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
    )

    registry = create_default_registry(review_context=context)

    names = {spec.name for spec in registry.list_specs()}
    assert {
        "read_file",
        "glob_files",
        "grep_files",
        "list_dir",
        "get_changed_context",
        "find_symbol_context",
        "validate_review_draft",
    } == names
    assert {spec.safety for spec in registry.list_specs()} == {ToolSafety.READONLY}


def test_default_registry_keeps_execute_mode_without_review_validator(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTE_ENABLED", "true")
    context = ReviewToolContext.from_diff(tmp_path, "")

    registry = create_default_registry(include_execute=True, review_context=context)

    names = {spec.name for spec in registry.list_specs()}
    assert {"run_command", "run_tests"}.issubset(names)
    assert "validate_review_draft" not in names
