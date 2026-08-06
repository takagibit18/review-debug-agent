"""Machine-readable Go/No-Go gate for formal paired Graph A/B collection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

VARIANTS = (
    "A-agent-search",
    "B1-graph-hybrid-cold",
    "B2-graph-hybrid-warm",
)
FROZEN_BASELINE_TARGET = "b5dc82bbffb38f1ba05587efa5dfcda08eb10b78"


def evaluate_gate(summary: dict[str, Any]) -> dict[str, Any]:
    """Evaluate engineering evidence and manual-review blockers."""
    blocking: list[str] = []
    warnings: list[str] = []

    def require(condition: bool, issue: str) -> None:
        if not condition:
            blocking.append(issue)

    smoke = summary.get("runs", {}).get("smoke", {})
    preflight = summary.get("runs", {}).get("preflight", {})
    preview = summary.get("runs", {}).get("preview", {})
    smoke_variants = smoke.get("variant_counts", {})
    preflight_variants = preflight.get("variant_counts", {})

    require(
        all(smoke_variants.get(variant, {}).get("valid") == 1 for variant in VARIANTS),
        "smoke_variants_incomplete_or_invalid",
    )
    require(
        summary.get("reviewed_preflight_fixture_count", 0) >= 3,
        "reviewed_preflight_fixture_count_below_3",
    )
    require(preflight.get("measured_runs") == 9, "preflight_measured_runs_not_9")
    require(
        all(
            preflight_variants.get(variant, {}).get("measured") == 3
            for variant in VARIANTS
        ),
        "preflight_variant_counts_incomplete",
    )
    require(
        all(
            preflight_variants.get(variant, {}).get("valid") == 3
            for variant in VARIANTS
        ),
        "preflight_variants_incomplete_or_invalid",
    )
    require(
        len(
            {
                preflight_variants.get(variant, {}).get("measured")
                for variant in VARIANTS
            }
        )
        == 1,
        "preflight_variant_counts_unequal",
    )

    for run_name, run in (("smoke", smoke), ("preflight", preflight)):
        for field in (
            "workspace_failures",
            "timeouts",
            "fallbacks",
            "schema_invalid",
        ):
            require(run.get(field) == 0, f"{run_name}_{field}")
        require(not run.get("pairing_errors"), f"{run_name}_pairing_errors")
        require(
            run.get("snapshot_pairing_consistent") is True,
            f"{run_name}_snapshot_pairing_inconsistent",
        )
        require(
            run.get("offline_restore_verified") is True,
            f"{run_name}_offline_restore_not_verified",
        )
        require(
            run.get("checkpoint_records_durable") is True,
            f"{run_name}_checkpoint_records_not_durable",
        )
        require(
            run.get("contract_invalid") == 0, f"{run_name}_variant_contract_invalid"
        )

    require(smoke.get("b1_all_cold") is True, "smoke_b1_not_all_cold")
    require(smoke.get("b2_all_warm") is True, "smoke_b2_not_all_warm")
    require(preflight.get("b1_all_cold") is True, "preflight_b1_not_all_cold")
    require(preflight.get("b2_all_warm") is True, "preflight_b2_not_all_warm")
    require(
        summary.get("checkpoint_resume_verified") is True,
        "checkpoint_resume_not_verified",
    )
    require(
        summary.get("structural_metrics_generated") is True,
        "structural_metrics_missing",
    )
    require(
        summary.get("held_out_executed") is False,
        "held_out_fixture_executed",
    )
    require(
        summary.get("frozen_baseline_target") == FROZEN_BASELINE_TARGET,
        "frozen_baseline_target_changed",
    )
    require(
        summary.get("frozen_baseline_modified") is False,
        "frozen_baseline_files_modified",
    )
    require(
        preview.get("measured_runs") == 15,
        "preview_measured_runs_not_15",
    )

    for fixture_id in summary.get("manual_review_pending", []):
        blocking.append(f"manual_review_pending: {fixture_id}")

    if preview.get("invalid_runs", 0):
        warnings.append(f"preview_invalid_runs: {preview['invalid_runs']}")
    if summary.get("formal_graph_ab") is False:
        warnings.append("engineering_preview_only: no formal statistical conclusion")
    return {
        "ready_for_formal_paired_ab": not blocking,
        "blocking_issues": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    result = evaluate_gate(summary)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if not result["ready_for_formal_paired_ab"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
