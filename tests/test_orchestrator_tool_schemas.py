"""Tests for orchestrator-owned tool schema conversion."""

from src.tools import create_default_registry
from src.orchestrator.tool_schemas import build_submit_tool_schemas, build_tool_schemas
from src.tools.base import ToolSafety, ToolSpec


def test_build_tool_schemas_from_tool_specs() -> None:
    spec = ToolSpec(
        name="read_file",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        safety=ToolSafety.READONLY,
    )

    schemas = build_tool_schemas([spec])
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "read_file"


def test_build_submit_tool_schemas_contains_expected_submit_tools() -> None:
    schemas = build_submit_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"submit_review", "submit_debug"}


def test_build_tool_schemas_from_default_registry_is_complete() -> None:
    schemas = build_tool_schemas(create_default_registry().list_specs())

    by_name = {schema["function"]["name"]: schema for schema in schemas}

    assert set(by_name) == {"read_file", "glob_files", "grep_files", "list_dir"}
    for schema in by_name.values():
        assert schema["type"] == "function"
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"
