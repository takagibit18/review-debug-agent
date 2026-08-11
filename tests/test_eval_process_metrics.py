"""Tests for v0.2.0 review process metrics and baseline comparison."""

from __future__ import annotations

import json
from pathlib import Path

import eval.run_summary as run_summary_module
from eval.schemas import EvalReport, EvalResult, MetricSummary


def test_extract_review_process_metrics_counts_review_lifecycle(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    events = [
        {
            "event_type": "finding_candidates_built",
            "phase": "verify_findings",
            "payload": {
                "candidate_count": 3,
                "evidence_bound_count": 2,
                "model_raw_issue_count": 4,
                "verifier_candidate_count": 3,
            },
        },
        {
            "event_type": "finding_verification_completed",
            "phase": "verify_findings",
            "payload": {
                "accepted_count": 1,
                "rejected_count": 1,
                "needs_evidence_count": 1,
                "downgraded_count": 0,
                "first_pass_accept_count": 1,
                "model_raw_issue_count": 4,
                "verifier_candidate_count": 3,
                "verifier_accepted_count": 1,
                "raw_accepted_count": 2,
                "raw_rejected_count": 0,
                "raw_needs_evidence_count": 1,
                "raw_downgraded_count": 0,
                "deterministic_evidence_checked_count": 2,
                "deterministic_evidence_passed_count": 1,
                "deterministic_evidence_rejected_count": 1,
            },
        },
        {
            "event_type": "workflow_summary",
            "phase": "workflow",
            "payload": {
                "required_step_count": 5,
                "completed_required_step_count": 4,
                "workflow_filtered_issue_count": 2,
                "final_effective_issue_count": 1,
                "workflow_invalid": True,
                "missing_required_steps": ["inspect_changed_context"],
            },
        },
        {
            "event_type": "finding_funnel_completed",
            "phase": "finding_funnel",
            "payload": {
                "submitted_finding_count": 4,
                "no_finding_run_count": 0,
                "non_risk_not_routed_count": 1,
                "pre_verifier_rejected_count": 0,
                "risk_candidate_count": 1,
                "filter_rescue_candidate_count": 1,
                "severity_calibration_candidate_count": 1,
                "calibration_rescue_candidate_count": 2,
                "semantic_rejected_count": 1,
                "deterministic_rejected_count": 1,
                "final_risk_finding_count": 1,
            },
        },
        {
            "event_type": "tool_io",
            "phase": "execute_tools",
            "payload": {"deduplicated": True},
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    extractor = getattr(run_summary_module, "extract_review_process_metrics", None)
    assert callable(extractor), "review process metric extractor is not implemented"
    metrics = extractor(log_path)

    assert metrics.candidate_issue_count == 3
    assert metrics.evidence_bound_issue_count == 2
    assert metrics.model_raw_issue_count == 4
    assert metrics.verifier_candidate_count == 3
    assert metrics.verifier_accepted_count == 1
    assert metrics.verifier_rejected_count == 1
    assert metrics.verifier_needs_evidence_count == 1
    assert metrics.first_pass_accept_count == 1
    assert metrics.raw_verifier_accepted_count == 2
    assert metrics.raw_verifier_rejected_count == 0
    assert metrics.raw_verifier_needs_evidence_count == 1
    assert metrics.deterministic_evidence_checked_count == 2
    assert metrics.deterministic_evidence_passed_count == 1
    assert metrics.deterministic_evidence_rejected_count == 1
    assert metrics.evidence_validation_pass_rate == 0.5
    assert metrics.required_step_count == 5
    assert metrics.completed_required_step_count == 4
    assert metrics.duplicate_tool_call_count == 1
    assert metrics.evidence_binding_rate == 2 / 3
    assert metrics.required_step_completion_rate == 0.8
    assert metrics.workflow_filtered_issue_count == 2
    assert metrics.final_effective_issue_count == 1
    assert metrics.workflow_invalid is True
    assert metrics.workflow_missing_steps == ["inspect_changed_context"]
    assert metrics.finding_funnel.submitted_finding_count == 4
    assert metrics.finding_funnel.non_risk_not_routed_count == 1
    assert metrics.finding_funnel.calibration_rescue_candidate_count == 2
    assert metrics.finding_funnel.semantic_rejected_count == 1
    assert metrics.finding_funnel.deterministic_rejected_count == 1
    assert metrics.finding_funnel.final_risk_finding_count == 1


def test_eval_result_serializes_zero_value_process_metrics() -> None:
    result = EvalResult(fixture_id="fixture", fixture_type="review")

    assert hasattr(result, "process_metrics")
    assert result.model_dump()["process_metrics"]["candidate_issue_count"] == 0


def test_metric_summary_aggregates_process_metrics() -> None:
    process_metrics_type = getattr(
        __import__("eval.schemas", fromlist=["ReviewProcessMetrics"]),
        "ReviewProcessMetrics",
        None,
    )
    assert process_metrics_type is not None, "ReviewProcessMetrics is not implemented"
    result = EvalResult(
        fixture_id="fixture",
        fixture_type="review",
        schema_valid=True,
        actual_count=1,
        false_positive_count=0,
        total_tokens=100,
        process_metrics=process_metrics_type(
            model_raw_issue_count=3,
            verifier_candidate_count=2,
            candidate_issue_count=2,
            evidence_bound_issue_count=2,
            verifier_accepted_count=1,
            verifier_rejected_count=1,
            raw_verifier_accepted_count=2,
            raw_verifier_rejected_count=0,
            deterministic_evidence_checked_count=2,
            deterministic_evidence_passed_count=1,
            deterministic_evidence_rejected_count=1,
            first_pass_accept_count=1,
            required_step_count=5,
            completed_required_step_count=5,
            duplicate_tool_call_count=1,
            workflow_filtered_issue_count=1,
            final_effective_issue_count=1,
            workflow_invalid=True,
            finding_funnel={
                "submitted_finding_count": 3,
                "pre_verifier_rejected_count": 1,
                "calibration_rescue_candidate_count": 1,
                "semantic_rejected_count": 1,
                "deterministic_rejected_count": 1,
                "final_risk_finding_count": 1,
            },
        ),
    )

    summary = MetricSummary.from_results([result])

    assert summary.evidence_binding_rate == 1.0
    assert summary.verifier_accept_rate == 0.5
    assert summary.verifier_reject_rate == 0.5
    assert summary.first_pass_accept_rate == 1.0
    assert summary.required_step_completion_rate == 1.0
    assert summary.duplicate_tool_call_rate == 0.5
    assert summary.cost_per_accepted_finding == 100.0
    assert summary.model_raw_issue_count == 3
    assert summary.verifier_candidate_count == 2
    assert summary.verifier_accepted_count == 1
    assert summary.verifier_rejected_count == 1
    assert summary.raw_verifier_accepted_count == 2
    assert summary.raw_verifier_rejected_count == 0
    assert summary.deterministic_evidence_checked_count == 2
    assert summary.deterministic_evidence_passed_count == 1
    assert summary.deterministic_evidence_rejected_count == 1
    assert summary.evidence_validation_pass_rate == 0.5
    assert summary.workflow_filtered_issue_count == 1
    assert summary.final_effective_issue_count == 1
    assert summary.workflow_invalid_run_count == 1
    assert summary.finding_funnel.submitted_finding_count == 3
    assert summary.finding_funnel.pre_verifier_rejected_count == 1
    assert summary.finding_funnel.calibration_rescue_candidate_count == 1
    assert summary.finding_funnel.semantic_rejected_count == 1
    assert summary.finding_funnel.deterministic_rejected_count == 1
    assert summary.finding_funnel.final_risk_finding_count == 1


def test_compare_reports_rejects_quality_and_cost_regression() -> None:
    compare_module = __import__("eval.compare", fromlist=["compare_reports"])
    baseline = EvalReport(
        suite="golden",
        metrics=MetricSummary(
            hit_rate=0.8,
            false_positive_rate=0.1,
            p95_latency_seconds=10,
            p95_total_tokens=100,
        ),
    )
    candidate = EvalReport(
        suite="golden",
        metrics=MetricSummary(
            hit_rate=0.7,
            false_positive_rate=0.2,
            p95_latency_seconds=17,
            p95_total_tokens=160,
        ),
    )

    comparison = compare_module.compare_reports(baseline, candidate)

    assert comparison.passed is False
    assert comparison.hit_rate_delta == -0.1
    assert comparison.false_positive_rate_delta == 0.1
    assert comparison.p95_latency_delta_ratio == 0.7
    assert comparison.p95_tokens_delta_ratio == 0.6
    assert set(comparison.failures) == {
        "hit_rate_regression",
        "false_positive_rate_regression",
        "p95_latency_regression",
        "p95_tokens_regression",
    }
