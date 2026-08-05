"""Issue-level structural grouping and Graph A/B aggregation tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval.graph_ab_pilot import VARIANT_IDS, compact_summary
from eval.runner import _structural_issue_metrics
from eval.schemas import (
    EvalIssueMatch,
    EvalResult,
    Fixture,
    MetricSummary,
    StructuralIssueMetrics,
)


def _mixed_fixture() -> Fixture:
    return Fixture.model_validate(
        {
            "id": "mixed-structure",
            "type": "review",
            "source": {"repo_full_name": "acme/repo", "pr_number": 1},
            "input": {"diff_text": "", "files": {}},
            "expected": {
                "issues": [
                    {
                        "path": "local.py",
                        "structural_scope": "local",
                        "graph_observable": False,
                    },
                    {
                        "path": "direct.py",
                        "structural_scope": "direct_cross_file",
                        "graph_observable": True,
                    },
                    {
                        "path": "hop.py",
                        "structural_scope": "multi_hop",
                        "graph_observable": True,
                    },
                    {"path": "legacy.py"},
                ]
            },
        }
    )


def _mixed_metrics() -> StructuralIssueMetrics:
    fixture = _mixed_fixture()
    matches = [
        EvalIssueMatch(expected_index=0, matched=True, matched_actual_index=0),
        EvalIssueMatch(expected_index=1, matched=False),
        EvalIssueMatch(expected_index=2, matched=True, matched_actual_index=1),
        EvalIssueMatch(expected_index=3, matched=False),
    ]
    return _structural_issue_metrics(fixture, matches)


def test_local_issue_is_counted_in_local_recall() -> None:
    metrics = _mixed_metrics()
    assert (metrics.local_matched_count, metrics.local_expected_count) == (1, 1)
    assert metrics.local_recall == 1.0


def test_direct_cross_file_issue_is_counted_separately() -> None:
    metrics = _mixed_metrics()
    assert (
        metrics.direct_cross_file_matched_count,
        metrics.direct_cross_file_expected_count,
    ) == (0, 1)
    assert metrics.direct_cross_file_recall == 0.0


def test_multi_hop_issue_is_counted_in_multi_hop_recall() -> None:
    metrics = _mixed_metrics()
    assert (metrics.multi_hop_matched_count, metrics.multi_hop_expected_count) == (
        1,
        1,
    )
    assert metrics.multi_hop_recall == 1.0


def test_graph_observable_true_has_its_own_denominator() -> None:
    metrics = _mixed_metrics()
    assert (
        metrics.graph_observable_matched_count,
        metrics.graph_observable_expected_count,
    ) == (1, 2)
    assert metrics.graph_observable_recall == 0.5


def test_graph_observable_false_is_not_treated_as_null() -> None:
    metrics = _mixed_metrics()
    assert (
        metrics.graph_unobservable_matched_count,
        metrics.graph_unobservable_expected_count,
    ) == (1, 1)
    assert metrics.graph_unobservable_recall == 1.0


def test_null_annotations_do_not_enter_group_denominators() -> None:
    metrics = _mixed_metrics()
    grouped = (
        metrics.local_expected_count
        + metrics.direct_cross_file_expected_count
        + metrics.multi_hop_expected_count
    )
    assert grouped == 3
    assert metrics.expected_count == 4


def test_overall_recall_includes_null_annotated_issue() -> None:
    metrics = _mixed_metrics()
    assert (metrics.matched_count, metrics.expected_count) == (2, 4)
    assert metrics.overall_recall == 0.5


def test_mixed_fixture_is_grouped_per_issue_not_by_fixture() -> None:
    metrics = _mixed_metrics()
    assert metrics.local_recall == 1.0
    assert metrics.direct_cross_file_recall == 0.0
    assert metrics.multi_hop_recall == 1.0


def test_grouped_recall_uses_expected_issue_denominators() -> None:
    metrics = _mixed_metrics()
    assert metrics.graph_observable_recall == pytest.approx(1 / 2)
    assert metrics.graph_unobservable_recall == pytest.approx(1 / 1)


def test_annotation_coverage_counts_only_non_null_values() -> None:
    metrics = _mixed_metrics()
    assert metrics.structural_annotation_coverage == 0.75
    assert metrics.graph_observability_annotation_coverage == 0.75


def test_old_fixture_schema_loads_with_null_structural_fields() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "legacy",
            "type": "review",
            "source": {"repo_full_name": "acme/repo", "pr_number": 2},
            "input": {"files": {}},
            "expected": {"issues": [{"path": "legacy.py"}]},
        }
    )
    issue = fixture.expected.issues[0]
    assert issue.structural_scope is None
    assert issue.graph_observable is None


def test_formal_candidates_have_evidence_based_issue_annotations() -> None:
    expected = {
        "golden_pydantic_pydantic_pr12117.json": [("local", False)],
        "golden_vybestack_llxprt-code_pr3012_reverse.json": [("multi_hop", True)],
        "golden_deepset-ai_haystack_pr12208_reverse.json": [("local", False)],
        "golden_real_requests_netrc_pr7205.json": [],
        "golden_pydantic_pydantic_pr12590.json": [],
    }
    for filename, expected_annotations in expected.items():
        fixture = Fixture.model_validate_json(
            (Path("eval/fixtures") / filename).read_text(encoding="utf-8")
        )
        assert [
            (issue.structural_scope, issue.graph_observable)
            for issue in fixture.expected.issues
        ] == expected_annotations


def test_metric_summary_aggregates_issue_counts_not_fixture_hit_rates() -> None:
    structural = _mixed_metrics()
    result = EvalResult(
        fixture_id="mixed-structure",
        fixture_type="review",
        schema_valid=True,
        expected_count=4,
        matched_count=2,
        false_positive_count=1,
        structural_metrics=structural,
    )
    summary = MetricSummary.from_results([result])
    assert summary.overall_recall == 0.5
    assert summary.precision == pytest.approx(2 / 3)
    assert summary.local_recall == 1.0
    assert summary.direct_cross_file_recall == 0.0
    assert summary.structural_annotation_coverage == 0.75


def _compact_payload(result: EvalResult) -> dict[str, Any]:
    records = []
    for order, variant_id in enumerate(VARIANT_IDS):
        variant_result = result.model_copy(update={"variant_id": variant_id})
        records.append(
            {
                "fixture_id": result.fixture_id,
                "fixture_types": ["mixed"],
                "repository_snapshot": "snapshot",
                "sample": 1,
                "order": order,
                "variant_id": variant_id,
                "run_id": f"run-{order}",
                "valid": True,
                "contract": {
                    "expected_variant_id": variant_id,
                    "expected_context_mode": variant_result.context_mode,
                    "expected_graph_cache_mode": variant_result.graph_cache_mode,
                    "actual_context_mode": variant_result.context_mode,
                    "actual_graph_status": "ready",
                    "actual_graph_cache_mode": variant_result.graph_cache_mode,
                    "actual_cache_hit": None,
                    "actual_manifest_count": 1,
                    "fallback_reason": "",
                    "valid": True,
                },
                "result": variant_result.model_dump(mode="json"),
            }
        )
    return {
        "experiment_id": "structural-test",
        "generated_at": "2026-08-05T00:00:00+00:00",
        "branch": "test",
        "start_commit": "a" * 40,
        "implementation_commit": "b" * 40,
        "frozen_baseline_tag": "eval/agent-baseline-v1",
        "frozen_baseline_target": "c" * 40,
        "formal_graph_ab": False,
        "held_out_executed": False,
        "seed": 1,
        "samples": 1,
        "suite": "test",
        "shared_contract": {},
        "pairing_errors": [],
        "records": records,
    }


def test_compact_summary_emits_structural_metrics_and_retained_quality() -> None:
    result = EvalResult(
        fixture_id="mixed-structure",
        fixture_type="review",
        schema_valid=True,
        expected_count=4,
        matched_count=2,
        false_positive_count=1,
        expected_root_cause_count=2,
        matched_root_cause_count=1,
        under_merge_count=1,
        repair_unit_expected_count=1,
        repair_unit_matched_count=1,
        structural_metrics=_mixed_metrics(),
    )

    summary = compact_summary(_compact_payload(result))
    variant = summary["variants"][VARIANT_IDS[0]]

    assert variant["structural_metrics"]["local_recall"] == 1.0
    assert variant["structural_metrics"]["multi_hop_recall"] == 1.0
    assert variant["structural_metrics"]["structural_annotation_coverage"] == 0.75
    assert variant["aggregate_quality"] == {
        "overall_recall": 0.5,
        "precision": pytest.approx(2 / 3),
        "root_cause_recall": 0.5,
        "over_merge_count": 0,
        "under_merge_count": 1,
        "repair_unit_accuracy": 1.0,
    }
