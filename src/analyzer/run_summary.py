"""Runtime run summaries derived from analyzer event logs and artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from src.analyzer.finding_funnel import FindingFunnel
from src.analyzer.schemas import ReviewOutcome

EventLogStatus = Literal["ok", "missing", "parse_error"]
PublishStatus = Literal["not_requested", "dry_run", "published", "failed"]


class RunSummary(BaseModel):
    """Compact diagnostic view of one event log."""

    run_id: str = ""
    event_log_path: str = ""
    event_log_status: EventLogStatus = "ok"
    parse_error: str = ""
    finish_reasons: list[str] = Field(default_factory=list)
    review_iterations: int = Field(default=0, ge=0)
    tool_bearing_iterations: int = Field(default=0, ge=0)
    submit_iteration: int | None = Field(default=None, ge=0)
    natural_completion: bool = False
    iteration_guard_hit: bool = False
    pre_budget_submit_triggered: bool = False
    termination_reason: str = ""
    budget_state: str = "none"
    submit_review_seen: bool = False
    submit_debug_seen: bool = False
    submit_validation_errors: list[str] = Field(default_factory=list)
    placeholder_summary: bool = False
    issues_count: int = 0
    tool_call_count: int = 0
    model_response_journal_writes: int = 0
    draft_findings_created: int = 0
    length_recoveries_attempted: int = 0
    length_recoveries_succeeded: int = 0
    length_recoveries_failed: int = 0
    model_names: list[str] = Field(default_factory=list)
    total_tokens: int = 0
    provider_attempt_count: int = 0
    successful_prompt_tokens: int = 0
    successful_completion_tokens: int = 0
    successful_reasoning_tokens: int = 0
    successful_total_tokens: int = 0
    successful_cached_prompt_tokens: int = 0
    successful_adjacent_common_prefix_tokens: int = 0
    cache_observation_count: int = 0
    provider_cache_hit_count: int = 0
    failed_attempt_count: int = 0
    failed_unknown_usage_count: int = 0
    publish_status: PublishStatus = "not_requested"
    model_raw_issue_count: int = 0
    verifier_candidate_count: int = 0
    finding_candidate_count: int = 0
    finding_accepted_count: int = 0
    finding_rejected_count: int = 0
    review_outcome: ReviewOutcome = "no_candidates"
    integrity_failure_codes: dict[str, list[str]] = Field(default_factory=dict)
    integrity_failure_details: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict
    )
    graph_available_path_count: int = 0
    graph_selected_path_count: int = 0
    graph_dropped_repeated_prefix_path_count: int = 0
    graph_selected_direct_path_count: int = 0
    graph_selected_production_path_count: int = 0
    graph_selected_low_hop_path_count: int = 0
    graph_required_production_path_count: int = 0
    graph_missing_production_path_count: int = 0
    graph_reviewer_context_token_estimate: int = 0
    graph_path_selection_reason_counts: dict[str, int] = Field(default_factory=dict)
    deterministic_evidence_checked_count: int = 0
    deterministic_evidence_passed_count: int = 0
    deterministic_evidence_rejected_count: int = 0
    workflow_enforcement: str = "off"
    workflow_required_step_count: int = 0
    workflow_completed_required_step_count: int = 0
    workflow_missing_steps: list[str] = Field(default_factory=list)
    workflow_reprompt_count: int = 0
    workflow_filtered_issue_count: int = 0
    final_effective_issue_count: int = 0
    workflow_invalid: bool = False
    finding_funnel: FindingFunnel = Field(default_factory=FindingFunnel)

    @computed_field(return_type=int)
    @property
    def actual_review_iterations(self) -> int:
        """Canonical observability name for the existing review_iterations field."""

        return self.review_iterations


class RunArtifactSummary(BaseModel):
    """Compact summary of runtime artifacts produced for one run."""

    run_id: str
    event_log: RunSummary
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    publish_status: PublishStatus = "not_requested"


def summarize_event_log(
    path: str | Path | None,
    *,
    run_id: str = "",
    publish_status: PublishStatus = "not_requested",
) -> RunSummary:
    """Read a JSONL event log and return a compact run summary."""
    if path is None:
        return RunSummary(
            run_id=run_id, event_log_status="missing", publish_status=publish_status
        )
    log_path = Path(path)
    summary = RunSummary(
        run_id=run_id,
        event_log_path=str(log_path),
        publish_status=publish_status,
    )
    if not log_path.exists():
        summary.event_log_status = "missing"
        return summary

    try:
        events = _load_events(log_path)
    except Exception as exc:  # noqa: BLE001
        summary.event_log_status = "parse_error"
        summary.parse_error = str(exc)
        return summary

    for event in events:
        _update_summary(summary, event)
    return summary


def summarize_run_artifacts(
    *,
    run_id: str,
    event_log_path: str | Path | None = None,
    response_json_path: str | Path | None = None,
    advisory_json_path: str | Path | None = None,
    publish_result_json_path: str | Path | None = None,
    publish_status: PublishStatus = "not_requested",
) -> RunArtifactSummary:
    """Build a stable artifact summary for CLI/API output."""
    paths: dict[str, str] = {}
    if event_log_path is not None:
        paths["event_log"] = str(event_log_path)
    if response_json_path is not None:
        paths["response_json"] = str(response_json_path)
    if advisory_json_path is not None:
        paths["advisory_json"] = str(advisory_json_path)
    if publish_result_json_path is not None:
        paths["publish_result_json"] = str(publish_result_json_path)
    return RunArtifactSummary(
        run_id=run_id,
        event_log=summarize_event_log(
            event_log_path,
            run_id=run_id,
            publish_status=publish_status,
        ),
        artifact_paths=paths,
        publish_status=publish_status,
    )


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def _update_summary(summary: RunSummary, event: dict[str, Any]) -> None:
    run_id = str(event.get("run_id", "") or "").strip()
    if run_id and not summary.run_id:
        summary.run_id = run_id
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return

    phase = str(event.get("phase", "") or "")
    event_type = str(event.get("event_type", "") or "")
    budget_state = str(payload.get("budget_state", "") or "")
    if budget_state and budget_state != "none":
        summary.budget_state = budget_state
    elif budget_state and summary.budget_state == "none":
        summary.budget_state = budget_state

    total_tokens = payload.get("total_tokens")
    if isinstance(total_tokens, int):
        summary.total_tokens = max(summary.total_tokens, total_tokens)

    if event_type == "model_response_detail":
        model = str(payload.get("model", "") or "").strip()
        if model and model not in summary.model_names:
            summary.model_names.append(model)
        usage = payload.get("usage")
        if (
            summary.provider_attempt_count == 0
            and isinstance(usage, dict)
            and isinstance(usage.get("total_tokens"), int)
        ):
            summary.total_tokens += int(usage["total_tokens"])
        tool_calls = payload.get("tool_call_summaries")
        if isinstance(tool_calls, list):
            summary.tool_call_count += len(tool_calls)

    if event_type == "model_call" and phase == "provider_attempt":
        summary.provider_attempt_count += 1
        success = payload.get("success") is True
        usage_present = payload.get("usage_present") is True
        if not success:
            summary.failed_attempt_count += 1
            if payload.get("usage_unknown") is True:
                summary.failed_unknown_usage_count += 1
        elif usage_present:
            summary.successful_prompt_tokens += _non_negative_int(
                payload.get("prompt_tokens")
            )
            summary.successful_completion_tokens += _non_negative_int(
                payload.get("completion_tokens")
            )
            summary.successful_reasoning_tokens += _non_negative_int(
                payload.get("reasoning_tokens")
            )
            summary.successful_total_tokens += _non_negative_int(
                payload.get("total_tokens")
            )
            summary.successful_cached_prompt_tokens += _non_negative_int(
                payload.get("cached_prompt_tokens")
            )
            summary.successful_adjacent_common_prefix_tokens += _non_negative_int(
                payload.get("adjacent_common_prefix_tokens")
            )
            if payload.get("cached_prompt_tokens") is not None:
                summary.cache_observation_count += 1
                if _non_negative_int(payload.get("cached_prompt_tokens")) > 0:
                    summary.provider_cache_hit_count += 1

    if event_type == "plan_parsed":
        summary.submit_review_seen = summary.submit_review_seen or bool(
            payload.get("submit_review_seen")
        )
        summary.submit_debug_seen = summary.submit_debug_seen or bool(
            payload.get("submit_debug_seen")
        )
        for key in ("submit_review_validation_error", "submit_debug_validation_error"):
            error = str(payload.get(key, "") or "").strip()
            if error and error not in summary.submit_validation_errors:
                summary.submit_validation_errors.append(error)

    if event_type == "format_result":
        summary.placeholder_summary = bool(payload.get("used_placeholder_summary"))
        issues_count = payload.get("issues_count")
        if isinstance(issues_count, int):
            summary.issues_count = max(summary.issues_count, issues_count)

    if event_type == "decision" and phase == "continue":
        reason = str(payload.get("reason", "") or "").strip()
        if reason and reason not in summary.finish_reasons:
            summary.finish_reasons.append(reason)
        if reason:
            normalized = _normalize_termination_reason(reason)
            if normalized:
                summary.termination_reason = normalized
                if normalized == "natural_model_stop":
                    summary.natural_completion = True
        if payload.get("reached_limit") is True:
            summary.iteration_guard_hit = True

    if event_type == "decision" and phase == "pre_budget_submit":
        summary.pre_budget_submit_triggered = True

    if payload.get("pre_budget_submit_triggered") is True:
        summary.pre_budget_submit_triggered = True

    if event_type == "phase_end" and phase == "review_complete":
        _update_graph_selection_summary(summary, payload)
        review_iterations = _non_negative_int(
            payload.get("review_iterations", payload.get("actual_review_iterations"))
        )
        if review_iterations:
            summary.review_iterations = review_iterations
        summary.tool_bearing_iterations = _non_negative_int(
            payload.get("tool_bearing_iterations")
        )
        submit_iteration = payload.get("submit_iteration")
        if submit_iteration is not None:
            summary.submit_iteration = _optional_non_negative_int(submit_iteration)
        if isinstance(payload.get("natural_completion"), bool):
            summary.natural_completion = payload["natural_completion"]
        if isinstance(payload.get("iteration_guard_hit"), bool):
            summary.iteration_guard_hit = payload["iteration_guard_hit"]
        if isinstance(payload.get("pre_budget_submit_triggered"), bool):
            summary.pre_budget_submit_triggered = payload[
                "pre_budget_submit_triggered"
            ]
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
                setattr(summary, field_name, _non_negative_int(payload[field_name]))
        termination_reason = str(payload.get("termination_reason", "") or "").strip()
        if termination_reason:
            summary.termination_reason = termination_reason
        summary.submit_review_seen = summary.submit_review_seen or bool(
            payload.get("submit_review_seen_any")
        )
        summary.model_response_journal_writes = int(
            payload.get("model_response_journal_writes", 0) or 0
        )
        summary.draft_findings_created = int(
            payload.get("draft_findings_created", 0) or 0
        )
        summary.length_recoveries_attempted = int(
            payload.get("length_recoveries_attempted", 0) or 0
        )
        summary.length_recoveries_succeeded = int(
            payload.get("length_recoveries_succeeded", 0) or 0
        )
        summary.length_recoveries_failed = int(
            payload.get("length_recoveries_failed", 0) or 0
        )

    if event_type == "finding_candidates_built":
        summary.model_raw_issue_count = int(
            payload.get("model_raw_issue_count", summary.model_raw_issue_count) or 0
        )
        summary.verifier_candidate_count = int(
            payload.get("verifier_candidate_count", payload.get("candidate_count", 0))
            or 0
        )
        summary.finding_candidate_count = int(payload.get("candidate_count", 0) or 0)

    if event_type == "context_plan_completed":
        _update_graph_selection_summary(summary, payload)

    if event_type == "finding_verification_completed":
        summary.model_raw_issue_count = int(
            payload.get("model_raw_issue_count", summary.model_raw_issue_count) or 0
        )
        summary.verifier_candidate_count = int(
            payload.get("verifier_candidate_count", summary.verifier_candidate_count)
            or 0
        )
        summary.finding_accepted_count = int(payload.get("accepted_count", 0) or 0)
        summary.finding_rejected_count = int(payload.get("rejected_count", 0) or 0)
        summary.deterministic_evidence_checked_count = int(
            payload.get("deterministic_evidence_checked_count", 0) or 0
        )
        summary.deterministic_evidence_passed_count = int(
            payload.get("deterministic_evidence_passed_count", 0) or 0
        )
        summary.deterministic_evidence_rejected_count = int(
            payload.get("deterministic_evidence_rejected_count", 0) or 0
        )
        raw_outcome = str(payload.get("review_outcome", "") or "")
        if raw_outcome in {
            "no_candidates",
            "accepted",
            "partially_rejected",
            "all_candidates_rejected",
        }:
            summary.review_outcome = raw_outcome  # type: ignore[assignment]
        raw_codes = payload.get("integrity_failures")
        if isinstance(raw_codes, dict):
            summary.integrity_failure_codes = {
                str(candidate_id): [str(code) for code in codes]
                for candidate_id, codes in raw_codes.items()
                if isinstance(codes, list)
            }
        raw_details = payload.get("integrity_failure_details")
        if isinstance(raw_details, dict):
            summary.integrity_failure_details = {
                str(candidate_id): [
                    dict(detail) for detail in details if isinstance(detail, dict)
                ]
                for candidate_id, details in raw_details.items()
                if isinstance(details, list)
            }
    if event_type == "finding_funnel_completed":
        summary.finding_funnel = FindingFunnel.model_validate(payload)

    if event_type == "workflow_summary":
        summary.workflow_enforcement = str(payload.get("enforcement", "off") or "off")
        summary.workflow_required_step_count = int(
            payload.get("required_step_count", 0) or 0
        )
        summary.workflow_completed_required_step_count = int(
            payload.get("completed_required_step_count", 0) or 0
        )
        missing = payload.get("missing_required_steps", [])
        summary.workflow_missing_steps = (
            [str(item) for item in missing] if isinstance(missing, list) else []
        )
        summary.workflow_reprompt_count = int(payload.get("reprompt_count", 0) or 0)
        summary.workflow_filtered_issue_count = int(
            payload.get("workflow_filtered_issue_count", 0) or 0
        )
        summary.final_effective_issue_count = int(
            payload.get("final_effective_issue_count", 0) or 0
        )
        summary.workflow_invalid = bool(payload.get("workflow_invalid", False))
        summary.workflow_filtered_issue_count = int(
            payload.get("workflow_filtered_issue_count", 0) or 0
        )
        summary.final_effective_issue_count = int(
            payload.get("final_effective_issue_count", 0) or 0
        )
        summary.workflow_invalid = bool(payload.get("workflow_invalid", False))


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


def _update_graph_selection_summary(
    summary: RunSummary, payload: dict[str, Any]
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
                setattr(summary, field_name, _non_negative_int(payload.get(key)))
                break

    raw_reasons = payload.get(
        "graph_path_selection_reason_counts",
        payload.get("path_selection_reason_counts"),
    )
    if isinstance(raw_reasons, dict):
        summary.graph_path_selection_reason_counts = {
            str(reason): _non_negative_int(count)
            for reason, count in raw_reasons.items()
        }
