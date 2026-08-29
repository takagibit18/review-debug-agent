"""Tests for the required-step review workflow state machine."""

from __future__ import annotations

import importlib

import pytest



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


def test_info_only_review_does_not_require_candidate_draft_validation() -> None:
    module = importlib.import_module("src.orchestrator.review_workflow")
    tracker = module.ReviewWorkflowTracker()
    tracker.complete("inspect_diff")
    tracker.complete("inspect_changed_context")
    tracker.complete("finalize_review")

    missing = tracker.missing_required(
        has_candidates=True,
        has_risk_candidates=False,
    )

    assert missing == []


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
