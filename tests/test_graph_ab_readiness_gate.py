"""Formal Graph A/B preflight configuration, summary, and gate tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from eval.formal_ab_readiness import render_report
from eval.graph_ab_gate import FROZEN_BASELINE_TARGET, VARIANTS, evaluate_gate
from eval.graph_ab_pilot import _fixture_entries, _load_config, _load_fixture

CONFIG = Path("eval/variants/graph-ab-formal-readiness.yaml")


def _run(*, fixtures: int, measured: int, per_variant: int) -> dict[str, Any]:
    return {
        "fixture_count": fixtures,
        "measured_runs": measured,
        "valid_runs": measured,
        "invalid_runs": 0,
        "invalid_run_details": [],
        "workspace_failures": 0,
        "fallbacks": 0,
        "timeouts": 0,
        "schema_invalid": 0,
        "contract_invalid": 0,
        "pairing_errors": [],
        "snapshot_pairing_consistent": True,
        "offline_restore_verified": True,
        "checkpoint_records_durable": True,
        "b1_all_cold": True,
        "b2_all_warm": True,
        "variant_counts": {
            variant: {"measured": per_variant, "valid": per_variant, "invalid": 0}
            for variant in VARIANTS
        },
    }


def _good_summary() -> dict[str, Any]:
    return {
        "formal_graph_ab": False,
        "held_out_executed": False,
        "frozen_baseline_target": FROZEN_BASELINE_TARGET,
        "frozen_baseline_modified": False,
        "reviewed_preflight_fixture_count": 3,
        "manual_review_pending": [],
        "checkpoint_resume_verified": True,
        "structural_metrics_generated": True,
        "runs": {
            "smoke": _run(fixtures=1, measured=3, per_variant=1),
            "preflight": _run(fixtures=3, measured=9, per_variant=3),
            "preview": _run(fixtures=5, measured=15, per_variant=5),
        },
    }


def test_formal_config_selects_exact_smoke_preflight_and_preview_sets() -> None:
    config = _load_config(CONFIG)

    smoke = _fixture_entries(config, "smoke")
    preflight = _fixture_entries(config, "preflight")
    preview = _fixture_entries(config, "preview")

    assert [fixture.id for fixture, _types, _phase in smoke] == [
        "development_agent_search_cross_file"
    ]
    assert [fixture.id for fixture, _types, _phase in preflight] == [
        "golden_real_requests_netrc_pr7205",
        "golden_pydantic_pydantic_pr12117",
        "golden_pydantic_pydantic_pr12590",
    ]
    assert len(preview) == 5
    assert {fixture.id for fixture, _types, _phase in preview} >= {
        "golden_vybestack_llxprt-code_pr3012_reverse",
        "golden_deepset-ai_haystack_pr12208_reverse",
    }


def test_reviewed_fixture_loads_without_engineering_preview_override() -> None:
    path = Path("eval/fixtures/golden_vybestack_llxprt-code_pr3012_reverse.json")

    fixture = _load_fixture(path)

    assert fixture.id == "golden_vybestack_llxprt-code_pr3012_reverse"
    assert fixture.metadata.annotated_by == "manual"
    assert fixture.metadata.reviewed is True


def test_formal_config_contains_no_held_out_fixture() -> None:
    config = _load_config(CONFIG)
    entries = _fixture_entries(config, "all")

    assert all(
        "held-out" not in {tag.lower() for tag in fixture.metadata.tags}
        for fixture, _types, _phase in entries
    )
    assert config["held_out_executed"] is False


def test_complete_engineering_evidence_passes_without_manual_blockers() -> None:
    result = evaluate_gate(_good_summary())

    assert result["ready_for_formal_paired_ab"] is True
    assert result["blocking_issues"] == []


def test_pending_reverse_goldens_force_honest_no_go() -> None:
    summary = _good_summary()
    summary["manual_review_pending"] = [
        "golden_vybestack_llxprt-code_pr3012_reverse",
        "golden_deepset-ai_haystack_pr12208_reverse",
    ]

    result = evaluate_gate(summary)

    assert result["ready_for_formal_paired_ab"] is False
    assert result["blocking_issues"] == [
        "manual_review_pending: golden_vybestack_llxprt-code_pr3012_reverse",
        "manual_review_pending: golden_deepset-ai_haystack_pr12208_reverse",
    ]


@pytest.mark.parametrize(
    ("path", "value", "blocker"),
    [
        (("runs", "smoke", "fallbacks"), 1, "smoke_fallbacks"),
        (
            ("runs", "preflight", "workspace_failures"),
            1,
            "preflight_workspace_failures",
        ),
        (("runs", "preflight", "timeouts"), 1, "preflight_timeouts"),
        (("runs", "preflight", "schema_invalid"), 1, "preflight_schema_invalid"),
        (("runs", "preflight", "b1_all_cold"), False, "preflight_b1_not_all_cold"),
        (("runs", "preflight", "b2_all_warm"), False, "preflight_b2_not_all_warm"),
        (("checkpoint_resume_verified",), False, "checkpoint_resume_not_verified"),
        (("structural_metrics_generated",), False, "structural_metrics_missing"),
        (("held_out_executed",), True, "held_out_fixture_executed"),
    ],
)
def test_gate_blocks_each_required_engineering_failure(
    path: tuple[str, ...], value: Any, blocker: str
) -> None:
    summary = deepcopy(_good_summary())
    target = summary
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    result = evaluate_gate(summary)

    assert result["ready_for_formal_paired_ab"] is False
    assert blocker in result["blocking_issues"]


def test_report_language_is_engineering_only_and_lists_blockers() -> None:
    summary = _good_summary() | {
        "branch": "experiment/graph-ab-formal-readiness",
        "start_commit": "d" * 40,
        "frozen_baseline_tag": "eval/agent-baseline-v1",
        "commits": [
            {"commit": str(index), "subject": f"commit {index}"}
            for index in range(1, 7)
        ],
        "workspace_prefetch": {
            "success": True,
            "success_count": 5,
            "fixture_count": 5,
        },
        "three_repeat_restore_verified_by_test": "test_three_repeat",
        "fixture_audit": [],
    }
    summary["manual_review_pending"] = ["draft-fixture"]
    for run in summary["runs"].values():
        run["quality_and_cost"] = {
            variant: {
                "aggregate_quality": {
                    "overall_recall": 1.0,
                    "precision": 1.0,
                    "root_cause_recall": 1.0,
                    "over_merge_count": 0,
                    "under_merge_count": 0,
                    "repair_unit_accuracy": 1.0,
                },
                "structural_metrics": {
                    "local_recall": 1.0,
                    "direct_cross_file_recall": None,
                    "multi_hop_recall": 1.0,
                    "graph_observable_recall": 1.0,
                    "graph_unobservable_recall": 1.0,
                    "structural_annotation_coverage": 1.0,
                    "graph_observability_annotation_coverage": 1.0,
                },
                "valid_runs": 1,
                "invalid_runs": 0,
                "stability": {
                    "end_to_end_latency_seconds": {"mean": 1.0},
                    "total_tokens": {"mean": 100.0},
                },
            }
            for variant in VARIANTS
        }
    summary["gate"] = evaluate_gate(summary)

    report = render_report(summary)

    assert "engineering-readiness and preview evidence only" in report
    assert "Ready for formal paired A/B: `NO`" in report
    assert "manual_review_pending: draft-fixture" in report
    assert "Graph is superior" not in report
