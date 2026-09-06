"""Deterministic coverage for Graph Hybrid token accounting and staged submit."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.event_log import EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.prompts import build_review_messages
from src.analyzer.reviewer_projection import project_manifest_for_reviewer
from src.analyzer.schemas import ReviewRequest
from src.analyzer.run_summary import summarize_event_log
from src.models.compat import ModelCallPolicy
from src.models.conversation import ModelConversation
from src.models.schemas import (
    DraftFinding,
    ModelConfig,
    ModelResponse,
    TokenUsage,
)
from src.orchestrator.agent_loop import AgentOrchestrator
from src.orchestrator.tool_schemas import build_submit_tool_schemas
from src.tools.base import ToolResult


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _graph_edge(
    edge_id: str,
    kind: str,
    *,
    source: str,
    target: str,
    path: str,
    line: int,
    derived_from_edge: str = "",
) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "source": source,
        "target": target,
        "kind": kind,
        "path": path,
        "line": line,
        "resolver": "ast",
        "confidence": 0.92,
        "confidence_tier": "ast",
        "evidence_eligibility": "strong",
        "reason": "same call site; CALLS does not prove runtime identity",
        "derived_from_edge": derived_from_edge,
    }


def _fixture_manifest() -> dict[str, Any]:
    return {
        "candidate_id": "C-1",
        "changed_anchor": {
            "file": "src/checkout.py",
            "line": 4,
            "end_line": 4,
            "symbol_id": "checkout",
            "change_kind": "logic",
            "hunk_text": "return apply_discount(total)",
        },
        "included_spans": [
            {
                "file": "src/checkout.py",
                "start_line": 4,
                "end_line": 4,
                "symbol_id": "checkout",
                "role": "changed_hunk",
                "content": "return apply_discount(total)",
                "retrieval_source": "git_diff",
                "context_hash": "hash-checkout-hunk",
                "token_cost": 12,
            },
            {
                "file": "src/checkout.py",
                "start_line": 1,
                "end_line": 6,
                "symbol_id": "checkout",
                "role": "enclosing_symbol",
                "content": "def checkout(total): ...",
                "retrieval_source": "relation_graph",
                "context_hash": "hash-checkout-symbol",
                "token_cost": 18,
            },
            {
                "file": "tests/test_checkout.py",
                "start_line": 5,
                "end_line": 7,
                "symbol_id": "test_checkout",
                "role": "related_test",
                "content": "def test_checkout(): assert checkout(100) == 90",
                "retrieval_source": "relation_graph",
                "context_hash": "hash-test",
                "token_cost": 14,
            },
            {
                "file": "src/discounts.py",
                "start_line": 1,
                "end_line": 3,
                "symbol_id": "apply_discount",
                "role": "execution_flow",
                "content": "def apply_discount(total): return total * 0.9",
                "retrieval_source": "relation_graph",
                "context_hash": "hash-discount",
                "token_cost": 15,
            },
        ],
        "included_graph_paths": [
            {
                "path_id": "P-tested",
                "node_ids": ["checkout", "test_checkout"],
                "edges": [
                    _graph_edge(
                        "tested-edge",
                        "TESTED_BY",
                        source="checkout",
                        target="test_checkout",
                        path="tests/test_checkout.py",
                        line=6,
                        derived_from_edge="test-call",
                    )
                ],
                "score": 0.91,
                "semantic_role": "related_test",
                "evidence_eligibility": "strong",
                "explanation": "Long repeated explanation that should not be sent.",
            },
            {
                "path_id": "P-called",
                "node_ids": ["checkout", "test_checkout"],
                "edges": [
                    _graph_edge(
                        "called-edge",
                        "CALLED_BY",
                        source="checkout",
                        target="test_checkout",
                        path="tests/test_checkout.py",
                        line=6,
                    )
                ],
                "score": 0.90,
                "semantic_role": "execution_flow",
                "evidence_eligibility": "strong",
                "explanation": "Reciprocal long explanation that is redundant.",
            },
            {
                "path_id": "P-flow",
                "node_ids": ["checkout", "apply_discount"],
                "edges": [
                    _graph_edge(
                        "flow-edge",
                        "CALLS",
                        source="checkout",
                        target="apply_discount",
                        path="src/checkout.py",
                        line=4,
                    )
                ],
                "score": 0.88,
                "semantic_role": "execution_flow",
                "evidence_eligibility": "strong",
                "explanation": "Call flow explanation.",
            },
        ],
        "excluded_low_confidence_paths": [{"path_id": "P-audit", "reason": "audit-only"}],
        "discarded_paths": [{"path_id": "P-discarded", "reason": "budget"}],
        "token_cost": 59,
        "char_cost": 900,
        "included_node_count": 4,
        "max_depth": 2,
        "available_graph_path_count": 5,
        "selected_reviewer_path_count": 3,
        "dropped_repeated_prefix_path_count": 1,
        "selected_direct_path_count": 1,
        "graph_reviewer_context_token_estimate": 190,
        "path_selection_reason_counts": {"selected": 3},
        "truncation_reasons": [],
        "parent_manifest_ids": [],
        "retrieval_provenance": [{"secret": "must-not-be-projected"}],
    }


def test_reviewer_projection_deduplicates_only_reciprocal_test_path() -> None:
    manifest = _fixture_manifest()
    original = copy.deepcopy(manifest)
    telemetry: dict[str, Any] = {}

    projection = project_manifest_for_reviewer(manifest, telemetry_sink=telemetry)

    assert manifest == original
    assert [path["path_id"] for path in projection["included_graph_paths"]] == [
        "P-tested",
        "P-flow",
    ]
    assert telemetry["semantic_duplicate_path_count"] == 1
    assert telemetry["dropped_semantic_duplicate_path_count"] == 1
    assert telemetry["dropped_path_ids"] == ["P-called"]
    assert telemetry["semantic_duplicate_paths"][0]["reason"] == (
        "dominated_by_tested_by"
    )
    assert telemetry["semantic_duplicate_prompt_token_cost"] == (
        telemetry["semantic_duplicate_paths"][0][
            "dropped_path_prompt_token_cost"
        ]
    )

    internal_kinds = {
        edge["kind"]
        for path in manifest["included_graph_paths"]
        for edge in path["edges"]
    }
    assert {"CALLED_BY", "TESTED_BY"} <= internal_kinds
    assert {"related_test", "execution_flow"} <= {
        span["role"] for span in projection["included_spans"]
    }

    projected_manifest_keys = set(projection)
    assert "token_cost" not in projected_manifest_keys
    assert "retrieval_provenance" not in projected_manifest_keys
    assert "explanation" not in projection["included_graph_paths"][0]


def test_graph_reviewer_prompt_compacts_visible_duplicates_and_preserves_roles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RELATION_GRAPH_REVIEWER_CONTEXT_TOKEN_BUDGET", "900")
    checkout = (
        "def checkout(total):\n"
        "    subtotal = total\n"
        "    if subtotal < 0:\n"
        "        return subtotal\n"
        "    return apply_discount(subtotal)\n"
        "\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "checkout.py").write_text(checkout, encoding="utf-8")
    diff = (
        "diff --git a/src/checkout.py b/src/checkout.py\n"
        "--- a/src/checkout.py\n"
        "+++ b/src/checkout.py\n"
        "@@ -4,1 +4,1 @@\n"
        "-    return total\n"
        "+        return apply_discount(subtotal)\n"
    )
    request = ReviewRequest(
        repo_path=str(tmp_path), diff_mode=True, diff_text=diff
    )
    manifest = _fixture_manifest()
    context = ContextState(candidate_context_manifests=[manifest])
    telemetry: dict[str, Any] = {}

    messages = build_review_messages(
        request,
        context,
        diff,
        {"src/checkout.py": checkout},
        prompt_token_budget=10_000,
        telemetry_sink=telemetry,
    )
    payload = json.loads(messages[1].content.split("\n", 1)[1])
    prompt_manifest = payload["candidate_context_manifests"][0]

    assert manifest["included_graph_paths"][1]["edges"][0]["kind"] == "CALLED_BY"
    assert len(prompt_manifest["included_graph_paths"]) == 2
    assert {path["path_id"] for path in prompt_manifest["included_graph_paths"]} == {
        "P-tested",
        "P-flow",
    }
    assert {span["role"] for span in prompt_manifest["included_spans"]} == {
        "related_test",
        "execution_flow",
    }
    assert telemetry["semantic_duplicate_path_count"] == 1
    assert telemetry["candidate_context_prompt_token_cost"] == telemetry[
        "graph_reviewer_projection"
    ]["estimated_tokens"]
    assert telemetry["graph_reviewer_context_token_estimate"] <= 900
    assert telemetry["graph_reviewer_projection"]["selected_role_coverage"] == [
        "execution_flow",
        "related_test",
    ]
    assert telemetry["graph_reviewer_projection"]["dropped_path_count"] == 0

    forbidden_manifest_fields = {
        "token_cost",
        "char_cost",
        "included_node_count",
        "max_depth",
        "available_graph_path_count",
        "selected_reviewer_path_count",
        "dropped_repeated_prefix_path_count",
        "selected_direct_path_count",
        "graph_reviewer_context_token_estimate",
        "path_selection_reason_counts",
        "retrieval_provenance",
    }
    assert not forbidden_manifest_fields.intersection(prompt_manifest)
    assert not any(
        forbidden_manifest_fields.intersection(span)
        for span in prompt_manifest["included_spans"]
    )
    assert not any(
        "explanation" in path
        for path in prompt_manifest["included_graph_paths"]
    )


class _TelemetryClient:
    def __init__(self, response: ModelResponse, attempts: list[dict[str, Any]]) -> None:
        self.default_config = ModelConfig(model="glm-5.3")
        self.response = response
        self.attempts = list(attempts)
        self.calls: list[list[Any]] = []
        self.tools: list[list[dict[str, Any]]] = []
        self.policies: list[ModelCallPolicy | None] = []
        self.configs: list[ModelConfig | None] = []

    async def chat(
        self,
        messages: list[Any],
        config: ModelConfig | None = None,
        tools: list[dict[str, Any]] | None = None,
        policy: ModelCallPolicy | None = None,
        conversation: Any = None,
    ) -> ModelResponse:
        del conversation
        self.calls.append(messages)
        self.tools.append(tools or [])
        self.policies.append(policy)
        self.configs.append(config)
        return self.response

    def consume_call_telemetry(self) -> list[dict[str, Any]]:
        attempts = self.attempts
        self.attempts = []
        return attempts


def test_submit_only_payload_has_minimal_evidence_and_records_attempt_telemetry(
    tmp_path: Path,
) -> None:
    manifest = _fixture_manifest()
    draft = DraftFinding(
        id="D-1",
        source_response_id="R-1",
        file="src/checkout.py",
        line=4,
        claim="The changed checkout call may alter the discount contract.",
    )
    response = ModelResponse(
        tool_calls=[
            _tool_call(
                "submit-1",
                "submit_review",
                {"summary": "No actionable issues found.", "issues": []},
            )
        ],
        usage=TokenUsage(
            prompt_tokens=17,
            completion_tokens=5,
            total_tokens=22,
            reasoning_tokens=2,
            cached_prompt_tokens=4,
        ),
        model="glm-5.3",
        finish_reason="tool_calls",
    )
    client = _TelemetryClient(
        response,
        [
            {
                "provider_attempt": 1,
                "thinking": "off",
                "actual_reasoning_effort": "low",
                "forced_tool": "submit_review",
                "tool_schema_count": 1,
                "prompt_tokens": 17,
                "completion_tokens": 5,
                "total_tokens": 22,
                "reasoning_tokens": 2,
                "cached_prompt_tokens": 4,
                "usage_present": True,
                "success": True,
                "provider_request_id": "req-1",
            }
        ],
    )
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    conversation = ModelConversation()
    conversation.add_assistant_tool_turn(
        response_id="old-response",
        content="old assistant turn",
        thinking="old hidden reasoning",
        tool_calls=[
            _tool_call(
                "old-read",
                "read_file",
                {"file_path": "src/old.py"},
            )
        ],
    )
    conversation.add_tool_result(
        "old-read", {"ok": True, "content": "old source should not be replayed"}
    )
    engine = InferenceEngine(
        client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
        conversation=conversation,
    )
    request = ReviewRequest(repo_path=str(tmp_path), diff_mode=True, diff_text="full diff")
    state = ContextState(
        goal="Run structured code review",
        candidate_context_manifests=[manifest],
        relation_graph_summary={"node_count": 4, "secret": "not a prompt component"},
    )
    validator_result = {
        "validated_draft_ids": ["D-1"],
        "validated_finding_ids": [],
        "validator_passed": True,
        "submit_allowed": True,
        "effective_issue_count": 0,
        "unresolved_evidence_gaps": [],
        "policy_warnings": [],
    }

    plan, usage = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=build_submit_tool_schemas(),
            diff_text="full diff",
            project_structure="full structure should not be resent",
            file_contents={"src/checkout.py": "full source should not be resent"},
            draft_findings=[draft],
            validator_result=validator_result,
            force_submit=True,
            stage="submit_only",
        )
    )

    assert plan.draft_review is not None
    assert usage.total_tokens == 22
    assert usage.reasoning_tokens == 2
    assert usage.cached_prompt_tokens == 4
    assert [schema["function"]["name"] for schema in client.tools[0]] == [
        "submit_review"
    ]
    assert client.policies[0] == ModelCallPolicy(
        thinking="off", forced_tool="submit_review"
    )
    assert client.configs[0] is not None and client.configs[0].max_tokens == 4096

    user_content = "\n".join(message.content for message in client.calls[0])
    assert "full diff" not in user_content
    assert "full structure should not be resent" not in user_content
    assert "full source should not be resent" not in user_content
    assert "old source should not be replayed" not in user_content
    assert not any(message.role in {"assistant", "tool"} for message in client.calls[0])
    assert "D-1" in user_content
    assert "C-1" in user_content
    assert "secret" not in user_content

    attempt_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.MODEL_CALL and phase == "provider_attempt"
    ]
    assert attempt_events == [
        {
            "iteration": 0,
            "provider_attempt": 1,
            "stage": "submit_only",
            "force_submit": True,
            "thinking": "off",
            "actual_reasoning_effort": "low",
            "forced_tool": "submit_review",
            "tool_schema_count": 1,
            "prompt_tokens": 17,
            "completion_tokens": 5,
            "total_tokens": 22,
            "reasoning_tokens": 2,
            "cached_prompt_tokens": 4,
            "request_hash": "",
            "request_estimated_tokens": 0,
            "adjacent_common_prefix_tokens": 0,
            "adjacent_prefix_hash": "",
            "provider_cache_hit": True,
            "usage_present": True,
            "success": True,
            "provider_request_id": "req-1",
            "usage_unknown": False,
        }
    ]
    context_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.CONTEXT_TELEMETRY and phase == "analyze"
    ]
    assert len(context_events) == 1
    component_names = {
        item["component"] for item in context_events[0]["component_records"]
    }
    assert {
        "system",
        "review_payload",
        "graph_manifest_projection",
        "graph_path_projection",
        "relation_graph_summary",
        "conversation_history",
        "tool_feedback",
        "final_submit_evidence",
        "defer_notice",
        "finalize_notice",
        "near_last_notice",
        "tool_schemas",
        "assembled_request_total",
    } <= component_names
    assert context_events[0]["stage"] == "submit_only"


def test_provider_failure_with_unknown_usage_is_not_a_successful_zero_token_call(
    tmp_path: Path,
) -> None:
    response = ModelResponse(
        usage=TokenUsage(prompt_tokens=11, completion_tokens=3, total_tokens=14),
        model="fake-model",
        finish_reason="stop",
    )
    client = _TelemetryClient(
        response,
        [
            {
                "provider_attempt": 1,
                "actual_reasoning_effort": "high",
                "failure_type": "ServiceUnavailableError",
                "failure_status": 500,
                "provider_code": "provider_unavailable",
                "usage_present": False,
                "usage_unknown": True,
                "success": False,
            },
            {
                "provider_attempt": 2,
                "actual_reasoning_effort": "high",
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
                "reasoning_tokens": 1,
                "usage_present": True,
                "usage_unknown": False,
                "success": True,
            },
        ],
    )
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )
    asyncio.run(
        engine.analyze(
            state=ContextState(goal="debug"),
            request=ReviewRequest(repo_path=str(tmp_path)),
            tool_specs=[],
            force_submit=True,
        )
    )
    attempt_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.MODEL_CALL and phase == "provider_attempt"
    ]
    assert len(attempt_events) == 2
    assert attempt_events[0]["success"] is False
    assert attempt_events[0]["failure_status"] == 500
    assert attempt_events[0]["usage_unknown"] is True
    assert attempt_events[0]["total_tokens"] == 0
    assert attempt_events[1]["success"] is True
    assert attempt_events[1]["total_tokens"] == 14

    event_path = tmp_path / "events.jsonl"
    event_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "run_id": "run-1",
                    "event_type": event_type.value,
                    "phase": phase,
                    "payload": payload,
                }
            )
            for event_type, phase, payload in events
        ),
        encoding="utf-8",
    )
    summary = summarize_event_log(event_path)
    assert summary.provider_attempt_count == 2
    assert summary.failed_attempt_count == 1
    assert summary.failed_unknown_usage_count == 1
    assert summary.successful_total_tokens == 14
    assert summary.successful_reasoning_tokens == 1


def test_model_client_parses_dict_usage_and_cached_prompt_tokens() -> None:
    from src.models.client import ModelClient

    response = ModelClient._parse_completion(
        {
            "id": "dict-response",
            "model": "fake-model",
            "choices": [
                {
                    "message": {
                        "content": "done",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 4,
                "total_tokens": 13,
                "completion_tokens_details": {"reasoning_tokens": 2},
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        }
    )

    assert response.content == "done"
    assert response.finish_reason == "stop"
    assert response.usage_present is True
    assert response.usage.prompt_tokens == 9
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 13
    assert response.usage.reasoning_tokens == 2
    assert response.usage.cached_prompt_tokens == 3


def test_validator_failure_returns_to_exploration_without_submit_only() -> None:
    orchestrator = AgentOrchestrator(
        review_workflow_enforcement="off",
        context_mode="agent_search",
    )
    orchestrator._review_stage = "validate"

    orchestrator._observe_validator_result(
        ToolResult(ok=False, error="missing evidence"),
        all_tools_succeeded=False,
    )

    assert orchestrator._review_stage == "explore"
    assert orchestrator._validator_passed is False
    assert orchestrator._last_validator_result is not None
    assert orchestrator._last_validator_result["submit_allowed"] is False


class _StageClient:
    def __init__(self) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self.calls: list[list[Any]] = []
        self.tools: list[list[dict[str, Any]]] = []
        self.policies: list[ModelCallPolicy | None] = []
        self.attempts: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Any],
        config: ModelConfig | None = None,
        tools: list[dict[str, Any]] | None = None,
        policy: ModelCallPolicy | None = None,
        conversation: Any = None,
    ) -> ModelResponse:
        del config, conversation
        index = len(self.calls)
        self.calls.append(messages)
        self.tools.append(tools or [])
        self.policies.append(policy)
        if index == 0:
            tool_calls = [
                _tool_call(
                    "draft-1",
                    "record_draft_finding",
                    {
                        "file": "src/checkout.py",
                        "line": 3,
                        "claim": "The changed checkout call may alter the contract.",
                    },
                ),
                _tool_call(
                    "read-1",
                    "read_file",
                    {"file_path": "src/checkout.py"},
                ),
            ]
        elif index == 1:
            tool_calls = [
                _tool_call(
                    "validate-1",
                    "validate_review_draft",
                    {
                        "summary": "No actionable issues found.",
                        "issues": [],
                        "draft_ids": ["D-1"],
                    },
                )
            ]
        else:
            tool_calls = [
                _tool_call(
                    "submit-1",
                    "submit_review",
                    {"summary": "No actionable issues found.", "issues": []},
                )
            ]
        self.attempts = [
            {
                "provider_attempt": 1,
                "thinking": policy.thinking if policy else "off",
                "actual_reasoning_effort": (
                    "low" if policy and policy.thinking == "off" else "high"
                ),
                "forced_tool": policy.forced_tool if policy else "none",
                "tool_schema_count": len(tools or []),
                "prompt_tokens": 10 + index,
                "completion_tokens": 4,
                "total_tokens": 14 + index,
                "reasoning_tokens": 1 if index < 2 else 0,
                "usage_present": True,
                "success": True,
                "provider_request_id": f"req-{index + 1}",
            }
        ]
        return ModelResponse(
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=10 + index,
                completion_tokens=4,
                total_tokens=14 + index,
                reasoning_tokens=1 if index < 2 else 0,
            ),
            model="fake-model",
            finish_reason="tool_calls",
        )

    def consume_call_telemetry(self) -> list[dict[str, Any]]:
        attempts = self.attempts
        self.attempts = []
        return attempts


def test_review_stage_runs_explore_validate_then_submit_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "events"))
    source = "def checkout(total):\n    return total\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "checkout.py").write_text(source, encoding="utf-8")
    orchestrator = AgentOrchestrator(
        review_max_iterations=5,
        review_min_tool_iterations=1,
        review_workflow_enforcement="off",
        context_mode="agent_search",
    )
    client = _StageClient()
    orchestrator._model_client = client  # type: ignore[assignment]
    request = ReviewRequest(
        repo_path=str(tmp_path),
        diff_mode=True,
        diff_text=(
            "diff --git a/src/checkout.py b/src/checkout.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def checkout(total):\n"
            "-    return total\n"
            "+    return total\n"
        ),
    )

    response = asyncio.run(orchestrator.run_review(request))

    assert response.report.issues == []
    assert len(client.calls) == 3
    assert [
        policy.forced_tool if policy else None for policy in client.policies
    ] == [None, None, "submit_review"]
    assert client.policies[2] == ModelCallPolicy(
        thinking="off", forced_tool="submit_review"
    )
    assert [schema["function"]["name"] for schema in client.tools[2]] == [
        "submit_review"
    ]

    event_path = next((tmp_path / "events").glob("**/*.jsonl"))
    lines = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    provider_stages = [
        event["payload"]["stage"]
        for event in lines
        if event["event_type"] == "model_call"
        and event["phase"] == "provider_attempt"
    ]
    assert provider_stages == ["explore", "validate", "submit_only"]
    logical_stages = [
        event["payload"]["stage"]
        for event in lines
        if event["event_type"] == "model_call" and event["phase"] == "analyze"
    ]
    assert logical_stages == ["explore", "validate", "submit_only"]
    complete = next(
        event
        for event in lines
        if event["event_type"] == "phase_end"
        and event["phase"] == "review_complete"
    )
    assert complete["payload"]["review_stage"] == "complete"
    assert complete["payload"]["provider_attempt_count"] == 3
    assert complete["payload"]["successful_total_tokens"] == 45
