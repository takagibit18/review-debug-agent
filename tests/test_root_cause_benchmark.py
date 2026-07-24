"""Deterministic ablation benchmark tests."""

from __future__ import annotations

import pytest

from eval.root_cause_benchmark import run_benchmark


def test_required_ablation_benchmark_reports_measured_quality_and_graph_metrics() -> (
    None
):
    report = run_benchmark(["A", "B", "C", "D", "F", "G"])

    assert report["provider_calls"] == 0
    assert report["ablations_run"] == ["A", "B", "C", "D", "F", "G"]
    baseline = report["results"]["A"]["metrics"]
    consolidated = report["results"]["D"]["metrics"]
    assert baseline["hit_rate"] == consolidated["hit_rate"] == 1.0
    assert baseline["false_positive_rate"] == consolidated["false_positive_rate"] == 0.0
    assert baseline["final_finding_count"] == 7
    assert consolidated["final_finding_count"] == 4
    assert baseline["finding_inflation_ratio"] == 1.75
    assert consolidated["finding_inflation_ratio"] == 1.0
    assert consolidated["root_cause_coverage"] == 1.0
    assert consolidated["over_merge_rate"] == 0.0
    assert consolidated["repair_unit_accuracy"] == 1.0
    assert report["results"]["F"]["metrics"]["graph_node_count"] > 0
    assert report["results"]["F"]["metrics"]["included_graph_paths"] > 0
    assert report["results"]["G"]["metrics"]["field_read_write_edge_count"] > 0
    assert report["results"]["G"]["metrics"]["persistent_cache_hit_rate"] == 1.0
    assert all(
        result["metrics"]["model_token_usage"] == 0
        for result in report["results"].values()
    )


def test_optional_lsp_ablation_records_ast_fallback_and_invalid_key_fails() -> None:
    report = run_benchmark(["I"])
    assert report["results"]["I"]["metrics"]["resolver_diagnostic_count"] >= 1
    assert report["results"]["I"]["metrics"]["bounded_execution_path_count"] >= 1

    with pytest.raises(ValueError, match="unknown ablation"):
        run_benchmark(["Z"])
