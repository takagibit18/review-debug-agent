"""Tests for orchestrator-owned tool schema conversion."""

from src.tools import create_default_registry
from src.orchestrator.tool_schemas import (
    build_draft_finding_tool_schema,
    build_submit_tool_schemas,
    build_tool_schemas,
)
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


def test_build_tool_schemas_removes_only_nonsemantic_generated_metadata() -> None:
    parameters = {
        "title": "LookupInput",
        "type": "object",
        "properties": {
            "mode": {
                "title": "Mode",
                "type": "string",
                "enum": ["definition", "all"],
                "default": "all",
            },
            "line": {
                "title": "Line",
                "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
                "default": None,
            },
            "summary": {"type": "string", "default": ""},
            "issues": {"type": "array", "items": {"type": "object"}, "default": []},
        },
        "required": ["mode"],
    }
    spec = ToolSpec(
        name="lookup",
        description="Lookup",
        parameters=parameters,
        safety=ToolSafety.READONLY,
    )

    schema = build_tool_schemas([spec])[0]["function"]["parameters"]

    assert "title" not in str(schema)
    assert schema["properties"]["mode"]["default"] == "all"
    assert "default" not in schema["properties"]["line"]
    assert "default" not in schema["properties"]["summary"]
    assert "default" not in schema["properties"]["issues"]
    assert schema["properties"]["mode"]["enum"] == ["definition", "all"]
    assert schema["properties"]["line"]["anyOf"][0]["minimum"] == 1
    assert schema["required"] == ["mode"]
    assert parameters["title"] == "LookupInput"


def test_build_submit_tool_schemas_contains_expected_submit_tools() -> None:
    schemas = build_submit_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"submit_review", "submit_debug"}


def test_draft_finding_schema_is_minimal_and_runtime_provenance_is_absent() -> None:
    schema = build_draft_finding_tool_schema()["function"]
    parameters = schema["parameters"]

    assert schema["name"] == "record_draft_finding"
    assert parameters["required"] == ["file", "claim"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"file", "claim", "line", "symbol"}
    forbidden = {
        "id",
        "draft_id",
        "source_response_id",
        "severity",
        "confidence",
        "root_cause",
        "impact",
        "candidate_id",
    }
    assert forbidden.isdisjoint(parameters["properties"])


def test_submit_review_schema_requires_explicit_issues_array() -> None:
    schemas = build_submit_tool_schemas()
    review_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "submit_review"
    )
    parameters = review_schema["function"]["parameters"]

    assert parameters["required"] == ["summary", "issues"]
    assert (
        "summary must not mention" in parameters["properties"]["summary"]["description"]
    )
    assert "Use [] only when" in parameters["properties"]["issues"]["description"]
    issue_schema = parameters["properties"]["issues"]["items"]
    assert (
        "concrete changed-code bugs"
        in issue_schema["properties"]["confidence"]["description"]
    )


def test_build_tool_schemas_from_default_registry_is_complete() -> None:
    schemas = build_tool_schemas(create_default_registry().list_specs())

    by_name = {schema["function"]["name"]: schema for schema in schemas}

    assert set(by_name) == {"read_file", "glob_files", "grep_files", "list_dir"}
    for schema in by_name.values():
        assert schema["type"] == "function"
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"


def test_build_tool_schemas_include_review_context_tools_when_registered(
    tmp_path,
) -> None:
    from src.tools.review_context import ReviewToolContext

    context = ReviewToolContext.from_diff(
        tmp_path,
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n",
    )
    schemas = build_tool_schemas(
        create_default_registry(review_context=context).list_specs()
    )

    by_name = {schema["function"]["name"]: schema for schema in schemas}

    assert {
        "get_changed_context",
        "find_symbol_context",
        "validate_review_draft",
    }.issubset(by_name)
    assert (
        by_name["get_changed_context"]["function"]["parameters"]["properties"][
            "radius"
        ]["maximum"]
        == 200
    )
