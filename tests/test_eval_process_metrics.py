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
            "payload": {"candidate_count": 3, "evidence_bound_count": 2},
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
            },
        },
        {
            "event_type": "workflow_summary",
            "phase": "workflow",
            "payload": {"required_step_count": 5, "completed_required_step_count": 4},
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
    assert metrics.verifier_accepted_count == 1
    assert metrics.verifier_rejected_count == 1
    assert metrics.verifier_needs_evidence_count == 1
    assert metrics.first_pass_accept_count == 1
    assert metrics.required_step_count == 5
    assert metrics.completed_required_step_count == 4
    assert metrics.duplicate_tool_call_count == 1
    assert metrics.evidence_binding_rate == 2 / 3
    assert metrics.required_step_completion_rate == 0.8


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
            candidate_issue_count=2,
            evidence_bound_issue_count=2,
            verifier_accepted_count=1,
            verifier_rejected_count=1,
            first_pass_accept_count=1,
            required_step_count=5,
            completed_required_step_count=5,
            duplicate_tool_call_count=1,
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

