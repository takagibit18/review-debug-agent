"""Tests for eval diagnostics classification."""

from eval.diagnostics import diagnose_result
from eval.schemas import EvalResult


def test_diagnose_hit_and_negative_false_positive() -> None:
    hit = diagnose_result(
        EvalResult(
            fixture_id="positive",
            fixture_type="review",
            schema_valid=True,
            expected_count=1,
            actual_count=1,
            matched_count=1,
        )
    )
    assert hit.reasons == ["event_log_missing", "hit"]

    fp = diagnose_result(
        EvalResult(
            fixture_id="negative",
            fixture_type="review",
            schema_valid=True,
            expected_count=0,
            actual_count=1,
            false_positive_count=1,
        )
    )
    assert "fp_negative" in fp.reasons


def test_diagnose_miss_no_issue_vs_filtered_issue() -> None:
    no_issue = diagnose_result(
        EvalResult(
            fixture_id="miss-empty",
            fixture_type="review",
            schema_valid=True,
            expected_count=1,
            actual_count=0,
            matched_count=0,
            raw_output={"report": {"issues": []}},
        )
    )
    assert "miss_no_issue" in no_issue.reasons
    assert "miss_filtered_issue" not in no_issue.reasons

    filtered = diagnose_result(
        EvalResult(
            fixture_id="miss-filtered",
            fixture_type="review",
            schema_valid=True,
            expected_count=1,
            actual_count=0,
            matched_count=0,
            raw_output={"report": {"issues": [{"severity": "warning"}]}},
        )
    )
    assert "miss_filtered_issue" in filtered.reasons


def test_diagnose_schema_placeholder_and_budget() -> None:
    diagnosis = diagnose_result(
        EvalResult(
            fixture_id="bad",
            fixture_type="review",
            schema_valid=False,
            expected_count=1,
            placeholder_summary=True,
            budget_state="hard_capped",
        )
    )

    assert "schema_invalid" in diagnosis.reasons
    assert "placeholder" in diagnosis.reasons
    assert "budget_capped" in diagnosis.reasons


def test_diagnose_workflow_invalid_gate_filter() -> None:
    diagnosis = diagnose_result(
        EvalResult(
            fixture_id="workflow-invalid",
            fixture_type="review",
            schema_valid=True,
            expected_count=1,
            actual_count=0,
            raw_output={"report": {"issues": [{"severity": "warning"}]}},
            workflow_invalid=True,
            workflow_missing_steps=["inspect_changed_context"],
        )
    )

    assert "workflow_invalid" in diagnosis.reasons
    assert "miss_filtered_issue" in diagnosis.reasons
