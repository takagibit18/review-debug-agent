"""Phase-one contracts for explicit Graph A/B context modes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from eval.runner import _match_issues, _semantic_text_matches
from eval.schemas import EVAL_MATCHER_VERSION, EvalVariant, Fixture
from src.analyzer.context_state import ContextState
from src.analyzer.context_strategy import (
    AgentSearchContextStrategy,
    GraphHybridContextStrategy,
)
from src.analyzer.event_log import EventType
from src.analyzer.finding_schema import RepairIntent, SourceAnchor
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.prompts import review_prompt_parts, review_system_prompt
from src.analyzer.schemas import (
    ReviewRequest,
    ReviewResponse,
)
from src.config import Settings


def _structured_agent_issue() -> ReviewIssue:
    return ReviewIssue(
        severity=Severity.WARNING,
        location="main.py:1",
        evidence="+return helper(value + 1)",
        suggestion="Increment in exactly one layer.",
        confidence=0.95,
        schema_version="2.0",
        finding_id="F-01",
        primary_anchor=SourceAnchor(file="main.py", line=1, symbol_id="main"),
        observed_behavior="The value is incremented twice.",
        causal_mechanism="Both caller and helper increment the same value.",
        violated_invariant="The input must be incremented exactly once.",
        repair_intent=RepairIntent(
            action="Remove one increment",
            targets=["main.call_helper", "helper.helper"],
            boundary="caller-helper contract",
        ),
    )


def _diff() -> str:
    return (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-return helper(value)\n"
        "+return helper(value + 1)\n"
    )


def test_agent_search_strategy_never_builds_graph_or_manifest(tmp_path: Path) -> None:
    request = ReviewRequest(repo_path=str(tmp_path), diff_mode=True, diff_text=_diff())
    result = asyncio.run(AgentSearchContextStrategy().prepare(request))

    assert result.context_mode == "agent_search"
    assert result.candidate_context_manifests == []
    assert result.graph_telemetry["graph_status"] == "disabled"
    assert result.graph_telemetry["graph_cache_mode"] == "not_applicable"
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_graph_hybrid_strategy_still_builds_valid_manifests(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "def run(value):\n    return helper(value + 1)\n", encoding="utf-8"
    )
    (tmp_path / "helper.py").write_text(
        "def helper(value):\n    return value + 1\n", encoding="utf-8"
    )
    events: list[EventType] = []
    strategy = GraphHybridContextStrategy(
        settings=Settings(review_context_mode="graph_hybrid"),
        workspace_root=tmp_path,
        relation_graph_index_path=tmp_path / "graph.sqlite3",
        record_event=lambda event_type, phase, payload: events.append(event_type),
    )
    result = asyncio.run(
        strategy.prepare(
            ReviewRequest(repo_path=str(tmp_path), diff_mode=True, diff_text=_diff())
        )
    )

    assert result.context_mode == "graph_hybrid"
    assert result.graph_telemetry["graph_status"] == "ready"
    assert result.candidate_context_manifests
    assert all(
        span.get("context_hash")
        for manifest in result.candidate_context_manifests
        for span in manifest.get("included_spans", [])
    )
    assert {
        "available_graph_path_count",
        "selected_reviewer_path_count",
        "dropped_repeated_prefix_path_count",
        "selected_direct_path_count",
        "graph_reviewer_context_token_estimate",
        "path_selection_reason_counts",
    } <= result.graph_telemetry.keys()
    assert EventType.RELATION_GRAPH_BUILT in events


def test_prompt_core_is_shared_and_mode_policy_isolated() -> None:
    agent_common, agent_policy = review_prompt_parts("agent_search")
    graph_common, graph_policy = review_prompt_parts("graph_hybrid")

    assert agent_common == graph_common
    assert "candidate_context_manifests" not in agent_common
    assert "No graph or candidate context manifest" in agent_policy
    assert "never invent graph provenance" in agent_policy
    assert "first-pass navigation context" in graph_policy
    assert "context_hash" in graph_policy
    assert agent_policy not in review_system_prompt("graph_hybrid")


def test_graph_policy_guides_joint_analysis_of_related_spans() -> None:
    agent_common, agent_policy = review_prompt_parts("agent_search")
    _, graph_policy = review_prompt_parts("graph_hybrid")

    joint_guidance = (
        "do not review them only in isolation",
        "An early local draft is not yet the root cause",
    )

    # Graph-specific joint-analysis guidance lives only in the graph policy.
    for phrase in joint_guidance:
        assert phrase in graph_policy
        # It must not leak into the agent_search policy...
        assert phrase not in agent_policy
        # ...nor into the shared common prompt core (unchanged by this change)...
        assert phrase not in agent_common
        # ...nor into the assembled agent_search system prompt.
        assert phrase not in review_system_prompt("agent_search")


def test_eval_matcher_is_variant_independent_and_semantic() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "semantic-fixture",
            "type": "review",
            "source": {"repo_full_name": "dev/local", "pr_number": 1},
            "input": {"diff_text": _diff(), "files": {}},
            "expected": {
                "issues": [
                    {
                        "severity": "warning",
                        "path": "main.py",
                        "line": 1,
                        "mechanism_pattern": "caller.*helper.*increment",
                        "invariant_pattern": "incremented exactly once",
                        "affected_paths": ["main.py"],
                    }
                ]
            },
        }
    )
    response = ReviewResponse(
        run_id="run",
        report=ReviewReport(issues=[_structured_agent_issue()]),
        context=ContextState(),
    )

    first = _match_issues(fixture, response)
    response.context.context_mode = "agent_search"
    second = _match_issues(fixture, response)

    assert first == second
    assert first[1:] == (1, 0)
    assert EVAL_MATCHER_VERSION == "semantic-v2"
    assert (
        EvalVariant(
            id="A-agent-search",
            context_mode="agent_search",
            graph_cache_mode="disabled",
        ).id
        == "A-agent-search"
    )


def test_eval_matcher_treats_second_time_as_duplicate_action() -> None:
    assert _semantic_text_matches(
        "discount.*twice|double.*discount|subtract.*again",
        "The caller subtracts the discount a second time after the helper.",
    )
