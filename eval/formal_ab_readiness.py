"""Build tracked formal-readiness summary and human-readable reports."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from eval.graph_ab_checkpoint import CheckpointJournal, StableRunKey
from eval.graph_ab_gate import evaluate_gate
from eval.graph_ab_pilot import (
    ROOT,
    VARIANT_IDS,
    PilotRunRecord,
    _checkpoint_status,
    compact_summary,
)
from eval.schemas import Fixture

START_COMMIT = "dc9ff2a91c2f95b5b1edb5b22a33150f026fe9ae"
FROZEN_BASELINE_TARGET = "b5dc82bbffb38f1ba05587efa5dfcda08eb10b78"
FROZEN_FILES = (
    "eval/contracts/agent-baseline-v1.yaml",
    "eval/baselines/agent-baseline-v1.json",
    "eval/reports/agent-baseline-v1.md",
)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return payload


def _prefetch_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["fixture_id"]): item
        for item in payload.get("records", [])
        if isinstance(item, dict) and item.get("fixture_id")
    }


def _checkpoint_evidence(
    payload: dict[str, Any], records: list[PilotRunRecord]
) -> bool:
    checkpoint = payload.get("checkpoint", {})
    raw_path = checkpoint.get("path") if isinstance(checkpoint, dict) else None
    contract_hash = payload.get("experiment_contract_hash")
    if not raw_path or not contract_hash:
        return False
    path = Path(str(raw_path))
    if not path.is_file():
        return False
    journal = CheckpointJournal(path)
    experiment_id = str(payload.get("experiment_id", ""))
    for record in records:
        key = StableRunKey(
            experiment_id=experiment_id,
            fixture_id=record.fixture_id,
            sample_index=record.sample,
            variant_id=record.variant_id,
            repository_snapshot=record.repository_snapshot,
            experiment_contract_hash=str(contract_hash),
        )
        durable = (
            journal.completed(key) if record.valid else journal.latest_failure(key)
        )
        if durable is None:
            return False
    return True


def _run_summary(
    payload: dict[str, Any], prefetch: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    records = [PilotRunRecord.model_validate(item) for item in payload["records"]]
    by_variant: dict[str, list[PilotRunRecord]] = defaultdict(list)
    for record in records:
        by_variant[record.variant_id].append(record)
    fixture_ids = list(dict.fromkeys(record.fixture_id for record in records))
    offline_verified = all(
        fixture_id == "development_agent_search_cross_file"
        or (
            prefetch.get(fixture_id, {}).get("success") is True
            and prefetch.get(fixture_id, {}).get("offline_checkout_verified") is True
        )
        for fixture_id in fixture_ids
    )
    compact = compact_summary(payload)

    def count_reason(reason: str) -> int:
        return sum(reason in record.invalid_reasons for record in records)

    b1 = by_variant["B1-graph-hybrid-cold"]
    b2 = by_variant["B2-graph-hybrid-warm"]
    return {
        "fixture_count": len(fixture_ids),
        "fixtures": fixture_ids,
        "measured_runs": len(records),
        "valid_runs": sum(record.valid for record in records),
        "invalid_runs": sum(not record.valid for record in records),
        "invalid_run_details": [
            {
                "fixture_id": record.fixture_id,
                "variant_id": record.variant_id,
                "reasons": record.invalid_reasons,
            }
            for record in records
            if not record.valid
        ],
        "workspace_failures": sum(
            _checkpoint_status(record) == "workspace_failure" for record in records
        ),
        "fallbacks": sum(
            bool(record.result.process_metrics.graph_fallback_reason)
            for record in records
        ),
        "timeouts": sum(
            "timeout" in record.invalid_reasons
            or "run_timeout" in record.result.finish_reasons
            for record in records
        ),
        "schema_invalid": sum(not record.result.schema_valid for record in records),
        "pairing_errors": list(payload.get("pairing_errors", [])),
        "snapshot_pairing_consistent": not any(
            "snapshot_mismatch" in str(item)
            for item in payload.get("pairing_errors", [])
        ),
        "offline_restore_verified": offline_verified,
        "checkpoint_records_durable": _checkpoint_evidence(payload, records),
        "checkpoint_reused_runs": int(
            payload.get("checkpoint", {}).get("reused_run_count", 0)
        ),
        "variant_counts": {
            variant: {
                "measured": len(by_variant[variant]),
                "valid": sum(record.valid for record in by_variant[variant]),
                "invalid": sum(not record.valid for record in by_variant[variant]),
            }
            for variant in VARIANT_IDS
        },
        "b1_all_cold": bool(b1)
        and all(
            record.result.process_metrics.graph_cache_mode == "cold"
            and record.result.process_metrics.graph_cache_hit is False
            for record in b1
        ),
        "b2_all_warm": bool(b2)
        and all(
            record.result.process_metrics.graph_cache_mode == "warm"
            and record.result.process_metrics.graph_cache_hit is True
            for record in b2
        ),
        "contract_invalid": count_reason("variant_id_mismatch")
        + count_reason("context_mode_mismatch"),
        "quality_and_cost": compact["variants"],
    }


def _fixture_audit(config_path: Path) -> tuple[int, list[str], list[dict[str, Any]]]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    reviewed_preflight = 0
    pending: list[str] = []
    rows: list[dict[str, Any]] = []
    for item in config.get("fixtures", []):
        path = (ROOT / str(item["path"])).resolve()
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        phase = str(item.get("phase"))
        if phase == "preflight" and fixture.metadata.reviewed:
            reviewed_preflight += 1
        if phase == "preview" and not fixture.metadata.reviewed:
            pending.append(fixture.id)
        rows.append(
            {
                "fixture_id": fixture.id,
                "phase": phase,
                "reviewed": fixture.metadata.reviewed,
                "expected_issue_count": len(fixture.expected.issues),
            }
        )
    return reviewed_preflight, pending, rows


def build_summary(
    *,
    config_path: Path,
    smoke_payload: dict[str, Any],
    preflight_payload: dict[str, Any],
    preview_payload: dict[str, Any],
    prefetch_payload: dict[str, Any],
) -> dict[str, Any]:
    prefetch = _prefetch_records(prefetch_payload)
    reviewed_count, pending, fixture_rows = _fixture_audit(config_path)
    runs = {
        "smoke": _run_summary(smoke_payload, prefetch),
        "preflight": _run_summary(preflight_payload, prefetch),
        "preview": _run_summary(preview_payload, prefetch),
    }
    frozen_target = _git("rev-parse", "eval/agent-baseline-v1^{commit}")
    frozen_diff = _git("diff", "--name-only", "--", *FROZEN_FILES)
    task_commits = [
        {"commit": line.split("\t", 1)[0], "subject": line.split("\t", 1)[1]}
        for line in _git(
            "log", "--reverse", "--format=%H%x09%s", f"{START_COMMIT}..HEAD"
        ).splitlines()
        if "\t" in line
    ]
    task_commits.append(
        {
            "commit": "SELF",
            "subject": "test(eval): add formal ab preflight readiness gate",
        }
    )
    preview_structural = runs["preview"]["quality_and_cost"]
    structural_generated = all(
        "structural_metrics" in preview_structural.get(variant, {})
        for variant in VARIANT_IDS
    )
    summary: dict[str, Any] = {
        "experiment_id": "graph-ab-formal-readiness",
        "generated_at": datetime.now(UTC).isoformat(),
        "branch": _git("branch", "--show-current"),
        "start_commit": START_COMMIT,
        "implementation_commit_before_readiness_commit": _git("rev-parse", "HEAD"),
        "commits": task_commits,
        "formal_graph_ab": False,
        "engineering_preview_only": True,
        "held_out_executed": any(
            bool(payload.get("held_out_executed"))
            for payload in (smoke_payload, preflight_payload, preview_payload)
        ),
        "frozen_baseline_tag": "eval/agent-baseline-v1",
        "frozen_baseline_target": frozen_target,
        "frozen_baseline_modified": bool(frozen_diff)
        or frozen_target != FROZEN_BASELINE_TARGET,
        "reviewed_preflight_fixture_count": reviewed_count,
        "manual_review_pending": pending,
        "fixture_audit": fixture_rows,
        "workspace_prefetch": {
            "success": prefetch_payload.get("success"),
            "fixture_count": prefetch_payload.get("fixture_count"),
            "success_count": prefetch_payload.get("success_count"),
            "failure_count": prefetch_payload.get("failure_count"),
            "records": [
                {
                    "fixture_id": item.get("fixture_id"),
                    "checkout_sha": item.get("checkout_sha"),
                    "repository_snapshot": item.get("repository_snapshot"),
                    "cache_size_bytes": item.get("cache_size_bytes"),
                    "offline_checkout_verified": item.get(
                        "offline_checkout_verified", False
                    ),
                    "success": item.get("success", False),
                    "error": item.get("error"),
                }
                for item in prefetch_payload.get("records", [])
            ],
        },
        "runs": runs,
        "checkpoint_resume_verified": (
            runs["preflight"]["checkpoint_reused_runs"] >= 2
            and runs["preflight"]["checkpoint_records_durable"]
        ),
        "structural_metrics_generated": structural_generated,
        "three_repeat_restore_verified_by_test": (
            "tests/test_eval_workspace_cache.py::"
            "test_cache_supplements_commits_and_restores_offline_three_times"
        ),
        "test_evidence": {},
    }
    summary["gate"] = evaluate_gate(summary)
    return summary


def render_report(summary: dict[str, Any]) -> str:
    gate = summary["gate"]
    lines = [
        "# Graph A/B Formal Collection Readiness",
        "",
        "This is engineering-readiness and preview evidence only. It is not a formal statistical conclusion and makes no comparative superiority claim.",
        "",
        f"- Branch: `{summary['branch']}`",
        f"- Start commit: `{summary['start_commit']}`",
        f"- Frozen baseline: `{summary['frozen_baseline_tag']}` -> `{summary['frozen_baseline_target']}`",
        f"- Frozen baseline modified: `{summary['frozen_baseline_modified']}`",
        f"- Held-out executed: `{summary['held_out_executed']}`",
        "",
        "## Logical commits",
        "",
    ]
    lines.extend(
        f"{index}. `{item['commit']}` {item['subject']}"
        for index, item in enumerate(summary["commits"], start=1)
    )
    lines.extend(
        [
            "",
            "## Workspace cache and restore",
            "",
            "The runner uses a targeted bare partial-clone cache, materializes only selected snapshots for offline checkout, and publishes caches atomically. Raw caches remain ignored under `eval/outputs/`.",
            "",
            f"- Prefetch success: `{summary['workspace_prefetch']['success']}` ({summary['workspace_prefetch']['success_count']}/{summary['workspace_prefetch']['fixture_count']})",
            f"- Three-repeat restore test: `{summary['three_repeat_restore_verified_by_test']}`",
            "",
            "## Run evidence",
            "",
            "| Layer | Fixtures | Measured | Valid | Invalid | Workspace failures | Fallbacks | Pairing errors |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in ("smoke", "preflight", "preview"):
        run = summary["runs"][name]
        lines.append(
            f"| {name} | {run['fixture_count']} | {run['measured_runs']} | {run['valid_runs']} | {run['invalid_runs']} | {run['workspace_failures']} | {run['fallbacks']} | {len(run['pairing_errors'])} |"
        )
    lines.extend(
        [
            "",
            f"Checkpoint/resume verified: `{summary['checkpoint_resume_verified']}`.",
            "",
            "Lifecycle checks:",
            "",
        ]
    )
    for name in ("smoke", "preflight", "preview"):
        run = summary["runs"][name]
        lines.append(
            f"- {name}: B1 all cold=`{run['b1_all_cold']}`, B2 all warm=`{run['b2_all_warm']}`, offline restore=`{run['offline_restore_verified']}`, checkpoint durable=`{run['checkpoint_records_durable']}`."
        )
    lines.extend(
        [
            "",
            "Invalid runs:",
            "",
        ]
    )
    for name in ("smoke", "preflight", "preview"):
        for item in summary["runs"][name]["invalid_run_details"]:
            lines.append(
                f"- {name}: `{item['fixture_id']}` / `{item['variant_id']}` - reasons: `{', '.join(item['reasons'])}`"
            )
    if not any(
        summary["runs"][name]["invalid_run_details"]
        for name in ("smoke", "preflight", "preview")
    ):
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Structural metrics and quality/cost",
            "",
        ]
    )
    for variant in VARIANT_IDS:
        data = summary["runs"]["preview"]["quality_and_cost"][variant]
        structural = data["structural_metrics"]
        quality = data["aggregate_quality"]
        lines.extend(
            [
                f"### {variant}",
                "",
                f"- Overall recall: `{quality['overall_recall']}`; precision: `{quality['precision']}`; root-cause recall: `{quality['root_cause_recall']}`.",
                f"- Local/direct cross-file/multi-hop recall: `{structural['local_recall']}` / `{structural['direct_cross_file_recall']}` / `{structural['multi_hop_recall']}`.",
                f"- Graph observable/unobservable recall: `{structural['graph_observable_recall']}` / `{structural['graph_unobservable_recall']}`.",
                f"- Structural coverage: `{structural['structural_annotation_coverage']}`; observability coverage: `{structural['graph_observability_annotation_coverage']}`.",
                f"- Over/under merge: `{quality['over_merge_count']}` / `{quality['under_merge_count']}`; repair-unit accuracy: `{quality['repair_unit_accuracy']}`.",
                f"- Valid/invalid runs: `{data['valid_runs']}` / `{data['invalid_runs']}`; mean end-to-end latency: `{data['stability']['end_to_end_latency_seconds']['mean']}` seconds; mean total tokens: `{data['stability']['total_tokens']['mean']}`.",
                "",
            ]
        )
    lines.extend(
        [
            "## Golden review status",
            "",
            f"Reviewed preflight fixtures: `{summary['reviewed_preflight_fixture_count']}`.",
            "",
            "| Fixture | Phase | Reviewed | Expected issues |",
            "|---|---|---:|---:|",
        ]
    )
    lines.extend(
        f"| {item['fixture_id']} | {item['phase']} | {item['reviewed']} | {item['expected_issue_count']} |"
        for item in summary["fixture_audit"]
    )
    lines.append("")
    lines.extend(
        f"- Manual review pending: `{item}`"
        for item in summary["manual_review_pending"]
    )
    lines.extend(
        [
            "",
            "## Go / No-Go",
            "",
            f"Ready for formal paired A/B: `{'YES' if gate['ready_for_formal_paired_ab'] else 'NO'}`.",
            "",
            "Blocking issues:",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in gate["blocking_issues"])
    lines.extend(["", "Warnings:", ""])
    lines.extend(f"- {item}" for item in gate["warnings"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--prefetch", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--docs-report", type=Path, required=True)
    args = parser.parse_args()
    summary = build_summary(
        config_path=args.config.resolve(),
        smoke_payload=_load_json(args.smoke),
        preflight_payload=_load_json(args.preflight),
        preview_payload=_load_json(args.preview),
        prefetch_payload=_load_json(args.prefetch),
    )
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.docs_report.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report = render_report(summary)
    args.report.write_text(report, encoding="utf-8")
    args.docs_report.write_text(report, encoding="utf-8")
    print(json.dumps(summary["gate"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
