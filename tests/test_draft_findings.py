"""Tests for minimal durable draft-finding working state."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from src.analyzer.context_state import ContextState
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import (
    DraftFinding,
    DraftFindingInput,
    ModelConfig,
    ModelResponse,
    TokenUsage,
)
from src.orchestrator.agent_loop import AgentOrchestrator
from src.orchestrator.run_journal import RunJournal
from src.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSpec


def _tool_names(schemas: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(schema.get("function", {}).get("name", ""))
        for schema in schemas or []
        if isinstance(schema.get("function"), dict)
    }


def test_draft_finding_input_rejects_runtime_and_complex_fields() -> None:
    for forbidden_field, value in (
        ("id", "model-id"),
        ("source_response_id", "model-response"),
        ("severity", "warning"),
        ("confidence", 0.9),
        ("root_cause", "complex attribution"),
        ("impact", "broad impact"),
        ("candidate_id", "candidate"),
    ):
        with pytest.raises(ValidationError, match=forbidden_field):
            DraftFindingInput.model_validate(
                {
                    "file": "src/example.py",
                    "claim": "Comparison may use the wrong object.",
                    forbidden_field: value,
                }
            )


def test_inference_parses_draft_as_pseudo_tool_not_regular_tool() -> None:
    client = _OneResponseClient(
        ModelResponse(
            tool_calls=[
                {
                    "id": "draft-call",
                    "function": {
                        "name": "record_draft_finding",
                        "arguments": json.dumps(
                            {
                                "file": "src/example.py",
                                "line": 12,
                                "symbol": "compare",
                                "claim": "Comparison may use the wrong object.",
                            }
                        ),
                    },
                },
                {
                    "id": "read-call",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":"src/example.py"}',
                    },
                },
            ],
            model="fake-model",
            finish_reason="tool_calls",
        )
    )
    engine = InferenceEngine(
        client,  # type: ignore[arg-type]
        model_response_writer=lambda response, iteration: "rje_runtime_bound",
    )

    plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
        )
    )

    assert plan.source_response_id == "rje_runtime_bound"
    assert len(plan.draft_finding_calls) == 1
    assert plan.draft_finding_calls[0].model_dump() == {
        "file": "src/example.py",
        "claim": "Comparison may use the wrong object.",
        "line": 12,
        "symbol": "compare",
    }
    assert [call["function"]["name"] for call in plan.tool_calls] == ["read_file"]


def test_inference_rejects_model_controlled_draft_provenance() -> None:
    engine = InferenceEngine(_OneResponseClient(ModelResponse()))  # type: ignore[arg-type]

    plan, meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "record_draft_finding",
                    "arguments": json.dumps(
                        {
                            "file": "src/example.py",
                            "claim": "Claim",
                            "source_response_id": "forged",
                            "draft_id": "forged",
                        }
                    ),
                }
            }
        ],
        ReviewRequest(repo_path="."),
    )

    assert plan.draft_finding_calls == []
    assert plan.tool_calls == []
    assert meta["draft_finding_validation_errors"]


def test_validation_repair_keeps_draft_bound_to_original_response() -> None:
    class _DraftThenRepairClient:
        def __init__(self) -> None:
            self.default_config = ModelConfig(model="fake-model")
            self.calls = 0

        async def chat(
            self, messages, config=None, tools=None, policy=None, conversation=None
        ):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[
                        {
                            "function": {
                                "name": "record_draft_finding",
                                "arguments": json.dumps(
                                    {
                                        "file": "src/example.py",
                                        "claim": "Comparison may use the wrong peer.",
                                    }
                                ),
                            }
                        },
                        {
                            "function": {
                                "name": "submit_review",
                                "arguments": '{"summary":"missing issues"}',
                            }
                        },
                    ],
                    finish_reason="tool_calls",
                    model="fake-model",
                )
            return ModelResponse(
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_review",
                            "arguments": '{"summary":"repaired","issues":[]}',
                        }
                    }
                ],
                finish_reason="tool_calls",
                model="fake-model",
            )

    response_ids = iter(["rje_original", "rje_repair"])
    engine = InferenceEngine(
        _DraftThenRepairClient(),  # type: ignore[arg-type]
        model_response_writer=lambda response, iteration: next(response_ids),
    )

    plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.source_response_id == "rje_repair"
    assert plan.draft_finding_source_response_id == "rje_original"
    assert len(plan.draft_finding_calls) == 1
    assert plan.draft_review is not None


def test_finalize_context_prioritizes_drafts_before_evidence_without_private_thinking() -> (
    None
):
    draft = DraftFinding(
        id="df_runtime",
        source_response_id="rje_source",
        file="src/example.py",
        line=9,
        symbol="compare",
        claim="Comparison may use the wrong peer.",
    )
    message, telemetry = InferenceEngine._build_final_submit_evidence_summary(  # noqa: SLF001
        [
            {
                "iteration": 0,
                "tool_call": {
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":"src/example.py"}',
                    }
                },
                "result": ToolResult(ok=True, data={"content": "return left == right"}),
            }
        ],
        {},
        [draft],
        token_budget=1000,
    )

    assert message is not None
    assert message.content.index("df_runtime") < message.content.index("tool_evidence")
    assert "prior_analysis_concern" not in message.content
    assert telemetry["included_draft_finding_count"] == 1
    assert telemetry["included_tool_result_count"] == 1
    assert telemetry["included_concern_count"] == 0


class _OneResponseClient:
    """Return one configured response and record supplied schemas/messages."""

    def __init__(self, response: ModelResponse) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self.response = response
        self.tools: list[list[dict[str, Any]] | None] = []
        self.messages: list[list[Any]] = []

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        self.messages.append(messages)
        self.tools.append(tools)
        return self.response.model_copy(deep=True)


class _DraftThenSubmitClient:
    """Record a draft plus ordinary read, then submit an explicit empty review."""

    def __init__(self) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self.calls = 0
        self.messages: list[list[Any]] = []
        self.tools: list[list[dict[str, Any]] | None] = []

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        self.tools.append(tools)
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "draft-call",
                        "function": {
                            "name": "record_draft_finding",
                            "arguments": json.dumps(
                                {
                                    "file": "src/wrapper.py",
                                    "line": 21,
                                    "symbol": "SafeWrapper.__eq__",
                                    "claim": (
                                        "Equality may compare against the wrapper "
                                        "instead of the wrapped peer."
                                    ),
                                }
                            ),
                        },
                    },
                    {
                        "id": "inspect-call",
                        "function": {
                            "name": "inspect_draft_state",
                            "arguments": "{}",
                        },
                    },
                ],
                usage=TokenUsage(total_tokens=10),
                model="fake-model",
                finish_reason="tool_calls",
            )
        return ModelResponse(
            tool_calls=[
                {
                    "id": "submit-call",
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "Evidence did not support a final issue.",
                                "issues": [],
                            }
                        ),
                    },
                }
            ],
            usage=TokenUsage(total_tokens=10),
            model="fake-model",
            finish_reason="tool_calls",
        )


class _DraftStateInspectionTool(BaseTool):
    """Observe durable and in-memory draft state at ordinary tool execution time."""

    def __init__(self, orchestrator_ref: dict[str, AgentOrchestrator]) -> None:
        self._orchestrator_ref = orchestrator_ref
        self.observations: list[tuple[str, int]] = []

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="inspect_draft_state",
            description="Inspect test draft state",
        )

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        orchestrator = self._orchestrator_ref["value"]
        last = orchestrator._run_journal.last_entry()  # type: ignore[union-attr]  # noqa: SLF001
        self.observations.append(
            (
                last.type if last is not None else "",
                len(orchestrator._draft_finding_store),  # noqa: SLF001
            )
        )
        return {"observed": True}


def test_draft_is_journaled_and_stored_before_ordinary_tool_then_used_by_finalize(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    orchestrator_ref: dict[str, AgentOrchestrator] = {}
    inspection_tool = _DraftStateInspectionTool(orchestrator_ref)
    registry = ToolRegistry()
    registry.register(inspection_tool)
    client = _DraftThenSubmitClient()
    orchestrator = AgentOrchestrator(
        registry=registry,
        review_max_iterations=2,
        review_workflow_enforcement="off",
    )
    orchestrator_ref["value"] = orchestrator
    orchestrator._model_client = client  # type: ignore[assignment]  # noqa: SLF001

    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    journal_path = (
        tmp_path / ".mergewarden" / "runs" / response.run_id / "journal.jsonl"
    )
    entries = RunJournal(response.run_id, journal_path, fsync=False).replay()
    assert [entry.type for entry in entries] == [
        "model_response",
        "draft_finding",
        "tool_result",
        "model_response",
    ]
    draft_entry = entries[1]
    assert draft_entry.payload["id"].startswith("df_")
    assert draft_entry.payload["source_response_id"] == entries[0].id
    assert draft_entry.payload["file"] == "src/wrapper.py"
    assert set(draft_entry.payload) == {
        "id",
        "source_response_id",
        "file",
        "line",
        "symbol",
        "claim",
    }
    assert inspection_tool.observations == [("draft_finding", 1)]
    stored = orchestrator._draft_finding_store.all()  # noqa: SLF001
    assert [draft.model_dump(mode="json") for draft in stored] == [draft_entry.payload]

    final_context = next(
        message.content
        for message in client.messages[1]
        if "Known draft findings:" in message.content
    )
    assert draft_entry.payload["id"] in final_context
    assert "src/wrapper.py:21 (SafeWrapper.__eq__)" in final_context
    assert "Equality may compare against the wrapper" in final_context
    assert response.report.issues == []
    assert "did not support" in response.report.summary
    assert "record_draft_finding" in _tool_names(client.tools[0])
    assert _tool_names(client.tools[1]) == {"submit_review"}


class _ModeSchemaClient:
    """Return valid fallback JSON while recording mode-specific tool schemas."""

    def __init__(self) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self.tools: list[list[dict[str, Any]] | None] = []

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        self.tools.append(tools)
        is_review = "code reviewer" in str(messages[0].content).lower()
        return ModelResponse(
            content=(
                '{"summary":"No issues.","issues":[]}'
                if is_review
                else '{"summary":"Debug complete.","hypotheses":[],"steps":[]}'
            ),
            model="fake-model",
            finish_reason="stop",
        )


def test_draft_pseudo_tool_is_not_exposed_in_plan_or_debug_mode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")

    plan_client = _ModeSchemaClient()
    plan_orchestrator = AgentOrchestrator(permission_mode="plan")
    plan_orchestrator._model_client = plan_client  # type: ignore[assignment]  # noqa: SLF001
    asyncio.run(
        plan_orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path / "plan")))
    )

    debug_client = _ModeSchemaClient()
    debug_orchestrator = AgentOrchestrator()
    debug_orchestrator._model_client = debug_client  # type: ignore[assignment]  # noqa: SLF001
    asyncio.run(
        debug_orchestrator.run_debug(DebugRequest(repo_path=str(tmp_path / "debug")))
    )

    assert "record_draft_finding" not in _tool_names(plan_client.tools[0])
    assert "record_draft_finding" not in _tool_names(debug_client.tools[0])
