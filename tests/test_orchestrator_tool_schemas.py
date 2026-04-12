"""Tests for orchestrator-owned tool schema conversion."""

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
