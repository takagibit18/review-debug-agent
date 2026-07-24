"""Eval-specific wrappers around runtime run-summary helpers."""

from __future__ import annotations

from pathlib import Path
import json

from pydantic import BaseModel, Field

from eval.schemas import EvalReport, ReviewProcessMetrics
from src.analyzer.run_summary import RunSummary, summarize_event_log


class EvalRunSummaryReport(BaseModel):
    """Run summaries for every fixture in one eval report."""

    suite: str
    generated_at: str
    report_path: str = ""
    runs: list[RunSummary] = Field(default_factory=list)


def summarize_eval_report(
    report: EvalReport,
    *,
    report_path: str | Path | None = None,
) -> EvalRunSummaryReport:
    """Summarize all event logs referenced by an eval report."""
    return EvalRunSummaryReport(
        suite=report.suite,
        generated_at=report.generated_at,
        report_path=str(report_path or ""),
        runs=[summarize_event_log(item.event_log_path) for item in report.results],
    )


def extract_review_process_metrics(
    event_log_path: str | Path | None,
) -> ReviewProcessMetrics:
    """Extract process metrics from one JSONL event timeline."""
    metrics = ReviewProcessMetrics()
    if not event_log_path:
        return metrics
    path = Path(event_log_path)
    if not path.is_file():
        return metrics
    metrics.event_log_status = "ok"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            metrics.event_log_status = "parse_error"
            return metrics
        payload = event.get("payload", {}) or {}
        event_type = str(event.get("event_type", ""))
        if event_type == "finding_candidates_built":
            metrics.model_raw_issue_count = _non_negative_int(
                payload.get("model_raw_issue_count")
            )
            metrics.verifier_candidate_count = _non_negative_int(
                payload.get("verifier_candidate_count", payload.get("candidate_count"))
            )
            metrics.candidate_issue_count = _non_negative_int(
                payload.get("candidate_count")
            )
            metrics.evidence_bound_issue_count = _non_negative_int(
                payload.get("evidence_bound_count")
            )
            metrics.structured_hypothesis_count = _non_negative_int(
                payload.get("structured_hypothesis_count")
            )
            metrics.evidence_complete_count = _non_negative_int(
                payload.get("evidence_complete_count")
            )
        elif event_type == "finding_verification_completed":
            metrics.verifier_accepted_count = _non_negative_int(
                payload.get("accepted_count")
            )
            metrics.verifier_rejected_count = _non_negative_int(
                payload.get("rejected_count")
            )
            metrics.verifier_needs_evidence_count = _non_negative_int(
                payload.get("needs_evidence_count")
            )
            metrics.verifier_downgraded_count = _non_negative_int(
                payload.get("downgraded_count")
            )
            metrics.raw_verifier_accepted_count = _non_negative_int(
                payload.get("raw_accepted_count", payload.get("accepted_count"))
            )
            metrics.raw_verifier_rejected_count = _non_negative_int(
                payload.get("raw_rejected_count", payload.get("rejected_count"))
            )
            metrics.raw_verifier_needs_evidence_count = _non_negative_int(
                payload.get(
                    "raw_needs_evidence_count", payload.get("needs_evidence_count")
                )
            )
            metrics.raw_verifier_downgraded_count = _non_negative_int(
                payload.get("raw_downgraded_count", payload.get("downgraded_count"))
            )
            metrics.deterministic_evidence_checked_count = _non_negative_int(
                payload.get("deterministic_evidence_checked_count")
            )
            metrics.deterministic_evidence_passed_count = _non_negative_int(
                payload.get("deterministic_evidence_passed_count")
            )
            metrics.deterministic_evidence_rejected_count = _non_negative_int(
                payload.get("deterministic_evidence_rejected_count")
            )
            metrics.first_pass_accept_count = _non_negative_int(
                payload.get("first_pass_accept_count")
            )
            metrics.model_raw_issue_count = _non_negative_int(
                payload.get("model_raw_issue_count", metrics.model_raw_issue_count)
            )
            metrics.verifier_candidate_count = _non_negative_int(
                payload.get(
                    "verifier_candidate_count", metrics.verifier_candidate_count
                )
            )
        elif event_type == "workflow_summary":
            metrics.required_step_count = _non_negative_int(
                payload.get("required_step_count")
            )
            metrics.completed_required_step_count = _non_negative_int(
                payload.get("completed_required_step_count")
            )
            metrics.workflow_filtered_issue_count = _non_negative_int(
                payload.get("workflow_filtered_issue_count")
            )
            metrics.final_effective_issue_count = _non_negative_int(
                payload.get("final_effective_issue_count")
            )
            metrics.workflow_invalid = bool(payload.get("workflow_invalid", False))
            missing_steps = payload.get("missing_required_steps", [])
            metrics.workflow_missing_steps = (
                [str(item) for item in missing_steps]
                if isinstance(missing_steps, list)
                else []
            )
        elif event_type == "context_plan_completed":
            metrics.candidate_context_tokens = _non_negative_int(
                payload.get("token_cost")
            )
            metrics.included_graph_nodes = _non_negative_int(
                payload.get("included_node_count")
            )
            metrics.included_graph_paths = _non_negative_int(
                payload.get("included_path_count")
            )
            metrics.discarded_graph_paths = _non_negative_int(
                payload.get("discarded_path_count")
            )
        elif event_type == "index_lifecycle":
            metrics.graph_build_latency_seconds = _non_negative_float(
                payload.get("build_latency_seconds")
            )
            metrics.incremental_update_latency_seconds = _non_negative_float(
                payload.get("incremental_update_latency_seconds")
            )
            metrics.persistent_cache_hit_rate = _bounded_ratio(
                payload.get("cache_hit_rate")
            )
        elif event_type == "root_cause_consolidation_completed":
            metrics.consolidator_block_count = _non_negative_int(
                payload.get("block_count")
            )
            metrics.consolidator_average_block_size = _non_negative_float(
                payload.get("average_block_size")
            )
            metrics.consolidator_proposal_count = _non_negative_int(
                payload.get("proposal_count")
            )
            metrics.consolidator_accepted_cluster_count = _non_negative_int(
                payload.get("accepted_cluster_count")
            )
            metrics.consolidator_rejected_cluster_count = _non_negative_int(
                payload.get("rejected_cluster_count")
            )
            metrics.final_root_cause_count = _non_negative_int(
                payload.get("final_root_cause_count")
            )
            metrics.finding_inflation_ratio = _non_negative_float(
                payload.get("finding_inflation_ratio")
            )
            metrics.unused_context_ratio = _bounded_ratio(
                payload.get("unused_context_ratio")
            )
            metrics.edge_confidence_contribution = _bounded_ratio(
                payload.get("edge_confidence_contribution")
            )
            metrics.evidence_complete_count = _non_negative_int(
                payload.get("evidence_complete_count", metrics.evidence_complete_count)
            )
        elif event_type == "tool_io":
            metrics.reviewer_tool_call_count += 1
            if payload.get("deduplicated") is True:
                metrics.duplicate_tool_call_count += 1
    return metrics


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _non_negative_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _bounded_ratio(value: object) -> float:
    return min(1.0, _non_negative_float(value))
