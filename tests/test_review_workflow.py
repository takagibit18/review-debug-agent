"""Tests for the required-step review workflow state machine."""

from __future__ import annotations

import importlib
import asyncio
import json
from pathlib import Path

import pytest

from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import AnalysisPlan, ReviewRequest
from src.orchestrator.agent_loop import AgentOrchestrator


def test_review_workflow_enforces_phase_order_and_terminal_states() -> None:
    module = importlib.import_module("src.orchestrator.review_workflow")
    tracker = module.ReviewWorkflowTracker()

    with pytest.raises(ValueError, match="missing predecessor"):
        tracker.start("inspect_changed_context")

    tracker.start("inspect_diff")
    tracker.complete("inspect_diff")
    tracker.start("inspect_changed_context")
    tracker.complete("inspect_changed_context")

    with pytest.raises(ValueError, match="terminal"):
        tracker.start("inspect_diff")


def test_review_workflow_conditions_and_explicit_skips() -> None:
    module = importlib.import_module("src.orchestrator.review_workflow")
    tracker = module.ReviewWorkflowTracker()
    tracker.complete("inspect_diff")
    tracker.skip(
        "inspect_changed_context", "full_repo_review", condition_not_applicable=True
    )

    missing_without_candidates = tracker.missing_required(
        has_candidates=False,
        has_risk_candidates=False,
    )
    missing_with_risk = tracker.missing_required(
        has_candidates=True,
        has_risk_candidates=True,
    )

    assert [step.step_id for step in missing_without_candidates] == [
        "inspect_changed_context", "finalize_review"
    ]
    assert [step.step_id for step in missing_with_risk] == [
        "inspect_changed_context", "validate_candidate_draft",
        "semantic_verify_findings",
        "finalize_review",
    ]
    assert tracker.states["inspect_changed_context"].skip_reason == "full_repo_review"


def test_review_workflow_retry_is_bounded() -> None:
    module = importlib.import_module("src.orchestrator.review_workflow")
    tracker = module.ReviewWorkflowTracker()
    tracker.start("inspect_diff")
    tracker.fail("inspect_diff", "diff unavailable")
    tracker.retry("inspect_diff")
    tracker.fail("inspect_diff", "still unavailable")

    with pytest.raises(ValueError, match="attempt limit"):
        tracker.retry("inspect_diff")


def test_review_workflow_summary_counts_required_completion() -> None:
    module = importlib.import_module("src.orchestrator.review_workflow")
    tracker = module.ReviewWorkflowTracker()
    tracker.complete("inspect_diff")
    tracker.complete("inspect_changed_context")
    tracker.complete("validate_candidate_draft")
    tracker.complete("semantic_verify_findings")
    tracker.complete("finalize_review")

    summary = tracker.summary(has_candidates=True, has_risk_candidates=True)

    assert summary["required_step_count"] == 5
    assert summary["completed_required_step_count"] == 5
    assert summary["missing_required_steps"] == []


def test_orchestrator_workflow_enforce_blocks_risk_when_context_step_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")

    class AcceptingVerifier:
        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="Verified against the supplied evidence.",
                        verified_evidence=["missing.py:1"],
                    )
                    for item in candidates
                ]
            )

    orchestrator = AgentOrchestrator(
        finding_verifier=AcceptingVerifier(),
        finding_verifier_mode="enforce",
        review_workflow_enforcement="enforce",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="risk",
                issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                        location="missing.py:1",
                        evidence="`return missing_value` can escape the guard",
                        suggestion="Return only after the guard succeeds.",
                        confidence=0.95,
                    )
                ],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/missing.py b/missing.py\n"
                    "--- a/missing.py\n"
                    "+++ b/missing.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+return missing_value\n"
                ),
            )
        )
    )

    assert response.report.issues == []
    assert response.workflow_invalid is True
    assert response.workflow_missing_steps == ["inspect_changed_context"]
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    summary = next(event for event in events if event["event_type"] == "workflow_summary")
    assert "inspect_changed_context" in summary["payload"]["missing_required_steps"]
    assert summary["payload"]["reprompt_count"] == 1
    assert summary["payload"]["workflow_invalid"] is True
    assert summary["payload"]["model_raw_issue_count"] == 1
    assert summary["payload"]["verifier_candidate_count"] == 1
    assert summary["payload"]["verifier_accepted_count"] == 1
    assert summary["payload"]["workflow_filtered_issue_count"] == 1
    assert summary["payload"]["final_effective_issue_count"] == 0


def test_orchestrator_recovery_prefetch_preserves_accepted_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")
    changed = tmp_path / "changed.py"
    changed.write_text("return missing_value\n", encoding="utf-8")

    class AcceptingVerifier:
        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="Verified against the changed line.",
                        verified_evidence=["changed.py:1"],
                    )
                    for item in candidates
                ]
            )

    orchestrator = AgentOrchestrator(
        finding_verifier=AcceptingVerifier(),
        finding_verifier_mode="enforce",
        review_workflow_enforcement="enforce",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="risk",
                issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                        location="changed.py:1",
                        evidence="`return missing_value` escapes the guard",
                        suggestion="Return only after the guard succeeds.",
                        confidence=0.95,
                    )
                ],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/changed.py b/changed.py\n"
                    "--- a/changed.py\n"
                    "+++ b/changed.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+return missing_value\n"
                ),
            )
        )
    )

    assert response.workflow_invalid is False
    assert response.workflow_missing_steps == []
    assert len(response.report.issues) == 1
