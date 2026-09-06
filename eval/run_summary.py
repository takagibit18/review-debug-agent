"""Eval-specific wrappers around runtime run-summary helpers."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from eval.schemas import (
    DEFAULT_EVAL_MATCHER_VERSION,
    EvalReport,
    ReviewProcessMetrics,
)
from src.analyzer.finding_funnel import FindingFunnel
from src.analyzer.run_summary import RunSummary, summarize_event_log


class EvalRunSummaryReport(BaseModel):
    """Run summaries for every fixture in one eval report."""

    suite: str
    generated_at: str
    matcher_version: str = DEFAULT_EVAL_MATCHER_VERSION
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
        matcher_version=report.matcher_version,
        report_path=str(report_path or ""),
        runs=[summarize_event_log(item.event_log_path) for item in report.results],
    )


def extract_review_process_metrics(
    event_log_path: str | Path | None,
    *,
    matcher_version: str = DEFAULT_EVAL_MATCHER_VERSION,
) -> ReviewProcessMetrics:
    """Extract process metrics from one JSONL event timeline."""
    metrics = ReviewProcessMetrics(matcher_version=matcher_version)
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
        phase = str(event.get("phase", ""))
        payload_matcher_version = payload.get("matcher_version")
        if isinstance(payload_matcher_version, str) and payload_matcher_version:
            metrics.matcher_version = payload_matcher_version
        if event_type == "decision" and phase == "continue":
            reason = str(payload.get("reason", "") or "").strip()
            normalized = _normalize_termination_reason(reason)
            if normalized:
                metrics.termination_reason = normalized
                if normalized == "natural_model_stop":
                    metrics.natural_completion = True
            if payload.get("reached_limit") is True:
                metrics.iteration_guard_hit = True
        if event_type == "decision" and phase == "pre_budget_submit":
            metrics.pre_budget_submit_triggered = True
        if payload.get("pre_budget_submit_triggered") is True:
            metrics.pre_budget_submit_triggered = True
        if event_type == "finding_funnel_completed":
            metrics.finding_funnel = FindingFunnel.model_validate(payload)
        elif event_type == "finding_candidates_built":
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
            metrics.deterministic_evidence_checked_count = _non_negative_int(
                payload.get("deterministic_evidence_checked_count")
            )
            metrics.deterministic_evidence_passed_count = _non_negative_int(
                payload.get("deterministic_evidence_passed_count")
            )
            metrics.deterministic_evidence_rejected_count = _non_negative_int(
                payload.get("deterministic_evidence_rejected_count")
            )
            metrics.model_raw_issue_count = _non_negative_int(
                payload.get("model_raw_issue_count", metrics.model_raw_issue_count)
            )
            metrics.verifier_candidate_count = _non_negative_int(
                payload.get(
                    "verifier_candidate_count", metrics.verifier_candidate_count
                )
            )
            raw_outcome = str(payload.get("review_outcome", "") or "")
            if raw_outcome in {
                "no_candidates",
                "accepted",
                "partially_rejected",
                "all_candidates_rejected",
            }:
                metrics.review_outcome = raw_outcome  # type: ignore[assignment]
            raw_codes = payload.get("integrity_failures")
            if isinstance(raw_codes, dict):
                metrics.integrity_failure_codes = {
                    str(candidate_id): [str(code) for code in codes]
                    for candidate_id, codes in raw_codes.items()
                    if isinstance(codes, list)
                }
            raw_details = payload.get("integrity_failure_details")
            if isinstance(raw_details, dict):
                metrics.integrity_failure_details = {
                    str(candidate_id): [
                        dict(detail) for detail in details if isinstance(detail, dict)
                    ]
                    for candidate_id, details in raw_details.items()
                    if isinstance(details, list)
                }
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
            _update_graph_selection_metrics(metrics, payload)
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
        elif event_type == "context_telemetry":
            _update_graph_reviewer_metrics(metrics, payload)
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
        elif event_type == "model_call" and phase == "provider_attempt":
            metrics.provider_attempt_count += 1
            success = payload.get("success") is True
            usage_present = payload.get("usage_present") is True
            if not success:
                metrics.failed_attempt_count += 1
                if payload.get("usage_unknown") is True:
                    metrics.failed_unknown_usage_count += 1
            elif usage_present:
                metrics.successful_prompt_tokens += _non_negative_int(
                    payload.get("prompt_tokens")
                )
                metrics.successful_completion_tokens += _non_negative_int(
                    payload.get("completion_tokens")
                )
                metrics.successful_reasoning_tokens += _non_negative_int(
                    payload.get("reasoning_tokens")
                )
                metrics.successful_total_tokens += _non_negative_int(
                    payload.get("total_tokens")
                )
                metrics.successful_cached_prompt_tokens += _non_negative_int(
                    payload.get("cached_prompt_tokens")
                )
                metrics.successful_adjacent_common_prefix_tokens += (
                    _non_negative_int(
                        payload.get("adjacent_common_prefix_tokens")
                    )
                )
                if payload.get("cached_prompt_tokens") is not None:
                    metrics.cache_observation_count += 1
                    if _non_negative_int(payload.get("cached_prompt_tokens")) > 0:
                        metrics.provider_cache_hit_count += 1
        elif (
            event_type == "phase_end"
            and phase == "review_complete"
        ):
            mode = str(payload.get("context_mode", "graph_hybrid"))
            metrics.context_mode = (
                "agent_search" if mode == "agent_search" else "graph_hybrid"
            )
            metrics.model = str(payload.get("model", ""))
            metrics.review_iterations = _non_negative_int(
                payload.get(
                    "review_iterations", payload.get("actual_review_iterations")
                )
            )
            metrics.tool_call_count = _non_negative_int(payload.get("tool_call_count"))
            metrics.tool_bearing_iterations = _non_negative_int(
                payload.get("tool_bearing_iterations")
            )
            metrics.submit_iteration = _optional_non_negative_int(
                payload.get("submit_iteration")
            )
            if isinstance(payload.get("natural_completion"), bool):
                metrics.natural_completion = payload["natural_completion"]
            if isinstance(payload.get("iteration_guard_hit"), bool):
                metrics.iteration_guard_hit = payload["iteration_guard_hit"]
            if isinstance(payload.get("pre_budget_submit_triggered"), bool):
                metrics.pre_budget_submit_triggered = payload[
                    "pre_budget_submit_triggered"
                ]
            termination_reason = str(
                payload.get("termination_reason", "") or ""
            ).strip()
            if termination_reason:
                metrics.termination_reason = termination_reason
            metrics.model_response_journal_writes = _non_negative_int(
                payload.get("model_response_journal_writes")
            )
            metrics.draft_findings_created = _non_negative_int(
                payload.get("draft_findings_created")
            )
            metrics.length_recoveries_attempted = _non_negative_int(
                payload.get("length_recoveries_attempted")
            )
            metrics.length_recoveries_succeeded = _non_negative_int(
                payload.get("length_recoveries_succeeded")
            )
            metrics.length_recoveries_failed = _non_negative_int(
                payload.get("length_recoveries_failed")
            )
            metrics.grep_calls = _non_negative_int(payload.get("grep_calls"))
            metrics.read_file_calls = _non_negative_int(payload.get("read_file_calls"))
            metrics.symbol_lookup_calls = _non_negative_int(
                payload.get("symbol_lookup_calls")
            )
            metrics.reviewer_latency_seconds = _non_negative_float(
                payload.get("reviewer_latency_seconds")
            )
            metrics.verifier_latency_seconds = _non_negative_float(
                payload.get("verifier_latency_seconds")
            )
            metrics.consolidation_latency_seconds = _non_negative_float(
                payload.get("consolidation_latency_seconds")
            )
            metrics.end_to_end_latency_seconds = _non_negative_float(
                payload.get("end_to_end_latency_seconds")
            )
            metrics.prompt_tokens = _optional_non_negative_int(
                payload.get("prompt_tokens")
            )
            metrics.completion_tokens = _optional_non_negative_int(
                payload.get("completion_tokens")
            )
            metrics.total_tokens = _non_negative_int(payload.get("total_tokens"))
            for field_name in (
                "provider_attempt_count",
                "successful_prompt_tokens",
                "successful_completion_tokens",
                "successful_reasoning_tokens",
                "successful_total_tokens",
                "successful_cached_prompt_tokens",
                "successful_adjacent_common_prefix_tokens",
                "cache_observation_count",
                "provider_cache_hit_count",
                "failed_attempt_count",
                "failed_unknown_usage_count",
            ):
                if field_name in payload:
                    setattr(
                        metrics,
                        field_name,
                        _non_negative_int(payload.get(field_name)),
                    )
            metrics.graph_status = str(payload.get("graph_status", ""))
            metrics.graph_cache_mode = str(
                payload.get("graph_cache_mode", "not_applicable")
            )
            metrics.manifest_count = _non_negative_int(payload.get("manifest_count"))
            metrics.manifest_token_cost = _non_negative_int(
                payload.get("manifest_token_cost")
            )
            metrics.parsed_file_count = _optional_non_negative_int(
                payload.get("parsed_file_count")
            )
            metrics.graph_node_count = _optional_non_negative_int(
                payload.get("graph_node_count")
            )
            metrics.graph_edge_count = _optional_non_negative_int(
                payload.get("graph_edge_count")
            )
            raw_cache_hit = payload.get("cache_hit")
            metrics.graph_cache_hit = (
                bool(raw_cache_hit) if isinstance(raw_cache_hit, bool) else None
            )
            metrics.graph_fallback_reason = str(payload.get("fallback_reason", ""))
            _update_graph_selection_metrics(metrics, payload)
    return metrics


def _update_graph_selection_metrics(
    metrics: ReviewProcessMetrics, payload: dict[str, object]
) -> None:
    """Copy graph path diversity counters from planner or review telemetry."""

    field_keys = {
        "graph_available_path_count": (
            "graph_available_path_count",
            "available_graph_path_count",
        ),
        "graph_selected_path_count": (
            "graph_selected_path_count",
            "selected_reviewer_path_count",
            "included_graph_path_count",
            "included_path_count",
        ),
        "graph_dropped_repeated_prefix_path_count": (
            "graph_dropped_repeated_prefix_path_count",
            "dropped_repeated_prefix_path_count",
        ),
        "graph_selected_direct_path_count": (
            "graph_selected_direct_path_count",
            "selected_direct_path_count",
        ),
        "graph_selected_production_path_count": (
            "graph_selected_production_path_count",
            "selected_production_path_count",
        ),
        "graph_selected_low_hop_path_count": (
            "graph_selected_low_hop_path_count",
            "selected_low_hop_path_count",
        ),
        "graph_required_production_path_count": (
            "graph_required_production_path_count",
            "required_production_path_count",
        ),
        "graph_missing_production_path_count": (
            "graph_missing_production_path_count",
            "missing_production_path_count",
        ),
        "graph_reviewer_context_token_estimate": (
            "graph_reviewer_context_token_estimate",
        ),
    }
    for field_name, keys in field_keys.items():
        for key in keys:
            if key in payload:
                setattr(metrics, field_name, _non_negative_int(payload.get(key)))
                break

    raw_reasons = payload.get(
        "graph_path_selection_reason_counts",
        payload.get("path_selection_reason_counts"),
    )
    if isinstance(raw_reasons, dict):
        metrics.graph_path_selection_reason_counts = {
            str(reason): _non_negative_int(count)
            for reason, count in raw_reasons.items()
        }


def _update_graph_reviewer_metrics(
    metrics: ReviewProcessMetrics, payload: dict[str, object]
) -> None:
    """Accumulate the Graph parts that reached the reviewer prompt."""

    projection = payload.get("graph_reviewer_prompt_projection")
    if not isinstance(projection, dict):
        return
    metrics.graph_reviewer_available_path_count += _non_negative_int(
        projection.get("available_path_count")
    )
    metrics.graph_reviewer_selected_path_count += _non_negative_int(
        projection.get("selected_path_count")
    )
    metrics.graph_reviewer_dropped_path_count += _non_negative_int(
        projection.get("dropped_path_count")
    )
    selected_tokens = projection.get("selected_token_count")
    if selected_tokens is None:
        selected_tokens = projection.get("estimated_tokens")
    metrics.graph_reviewer_selected_token_count += _non_negative_int(
        selected_tokens
    )
    raw_roles = projection.get("selected_role_coverage")
    if isinstance(raw_roles, list):
        metrics.graph_reviewer_role_coverage = sorted(
            {
                *metrics.graph_reviewer_role_coverage,
                *(str(role) for role in raw_roles if str(role)),
            }
        )


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _normalize_termination_reason(reason: str) -> str:
    """Map legacy decision labels to the normalized observability enum."""

    return {
        "model_completed": "natural_model_stop",
        "completed": "natural_model_stop",
        "max_iterations": "iteration_guard",
        "budget_soft_capped": "token_soft_limit",
        "budget_hard_capped": "token_hard_limit",
        "run_timeout": "run_timeout",
        "model_timeout": "provider_timeout",
        "pre_budget_submit_attempted": "pre_budget_submit",
    }.get(reason, "")


def _non_negative_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _bounded_ratio(value: object) -> float:
    return min(1.0, _non_negative_float(value))
