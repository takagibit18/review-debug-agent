"""Phase-one contracts for explicit Graph A/B context modes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from eval.runner import _match_issues
from eval.schemas import EVAL_MATCHER_VERSION, EvalVariant, Fixture
from src.analyzer.context_state import ContextState
from src.analyzer.context_strategy import (
    AgentSearchContextStrategy,
    GraphHybridContextStrategy,
)
from src.analyzer.event_log import EventType
from src.analyzer.finding_schema import EvidenceProvenance, RepairIntent, SourceAnchor
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.prompts import review_prompt_parts, review_system_prompt
from src.analyzer.schemas import (
    AnalysisPlan,
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
    ReviewResponse,
)
from src.config import Settings
from src.orchestrator.agent_loop import AgentOrchestrator
from src.tools.base import ToolRegistry
from src.tools.file_read import FileReadTool


def _diff() -> str:
    return (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-return helper(value)\n"
        "+return helper(value + 1)\n"
    )


def _structured_agent_issue() -> ReviewIssue:
    cause = EvidenceProvenance(
        candidate_id="F-01",
        retrieval_source="git_diff",
        file="main.py",
        line=1,
        statement="The changed line increments before calling helper.",
    )
    contract = EvidenceProvenance(
        candidate_id="F-01",
        retrieval_source="read_file",
        file="helper.py",
        line=1,
        statement="helper already increments the input.",
    )
    return ReviewIssue(
        severity=Severity.WARNING,
        location="main.py:1",
        evidence="+return helper(value + 1)",
        suggestion="Increment in exactly one layer.",
        confidence=0.95,
        schema_version="2.0",
        finding_id="F-01",
        primary_anchor=SourceAnchor(file="main.py", line=1, symbol_id="main"),
        related_locations=[],
        observed_behavior="The value is incremented twice.",
        causal_mechanism="Both caller and helper increment the same value.",
        violated_invariant="The input must be incremented exactly once.",
        repair_intent=RepairIntent(
            action="Remove one increment",
            targets=["main.call_helper", "helper.helper"],
            boundary="caller-helper contract",
        ),
        cause_evidence=[cause],
        contract_evidence=[contract],
    )


class _AcceptVerifier:
    def __init__(self) -> None:
        self.call_count = 0

    async def verify(self, candidates, request, state, **kwargs):  # type: ignore[no-untyped-def]
        del request, state, kwargs
        self.call_count += 1
        return FindingVerificationBatch(
            results=[
                FindingVerification(
                    candidate_id=item.candidate_id,
                    status="accepted",
                    reason_codes=["verified"],
                    rationale="Diff and successful tool evidence support the finding.",
                    verified_evidence=["main.py:1"],
                )
                for item in candidates
            ]
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


def test_agent_search_end_to_end_uses_tool_verifier_and_consolidator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "main.py").write_text("return helper(value + 1)\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text(
        "def helper(value): return value + 1\n", encoding="utf-8"
    )
    registry = ToolRegistry()
    registry.register(FileReadTool())
    verifier = _AcceptVerifier()
    orchestrator = AgentOrchestrator(
        registry=registry,
        context_mode="agent_search",
        review_max_iterations=2,
        finding_verifier=verifier,
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
    )
    calls = 0

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        del state, request, tool_specs, kwargs
        calls += 1
        if calls == 1:
            return AnalysisPlan(
                needs_tools=True,
                tool_calls=[
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {"file_path": "helper.py", "offset": 0, "limit": 20}
                            ),
                        }
                    }
                ],
            )
        orchestrator._submit_review_seen_any = True
        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="Cross-file contract regression found.",
                issues=[_structured_agent_issue()],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(repo_path=str(tmp_path), diff_mode=True, diff_text=_diff())
        )
    )

    assert response.report.issues
    assert response.context.context_mode == "agent_search"
    assert response.context.candidate_context_manifests == []
    assert verifier.call_count == 1
    assert not list(tmp_path.rglob("*.sqlite*"))
    events = [
        json.loads(line)
        for line in (tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    event_types = {item["event_type"] for item in events}
    assert "relation_graph_built" not in event_types
    assert "index_lifecycle" not in event_types
    assert "finding_verification_completed" in event_types
    assert "root_cause_consolidation_completed" in event_types
    prepare = next(
        item["payload"]
        for item in events
        if item["event_type"] == "phase_start" and item["phase"] == "prepare"
    )
    assert prepare["context_mode"] == "agent_search"
    assert prepare["relation_graph_enabled"] is False
    telemetry = next(
        item["payload"]
        for item in events
        if item["event_type"] == "phase_end" and item["phase"] == "review_complete"
    )
    assert telemetry["context_mode"] == "agent_search"
    assert telemetry["read_file_calls"] == 1
    assert telemetry["graph_status"] == "disabled"
    assert telemetry["manifest_count"] == 0


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
