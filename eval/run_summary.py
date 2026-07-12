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
            metrics.first_pass_accept_count = _non_negative_int(
                payload.get("first_pass_accept_count")
            )
            metrics.model_raw_issue_count = _non_negative_int(
                payload.get("model_raw_issue_count", metrics.model_raw_issue_count)
            )
            metrics.verifier_candidate_count = _non_negative_int(
                payload.get("verifier_candidate_count", metrics.verifier_candidate_count)
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
        elif event_type == "tool_io" and payload.get("deduplicated") is True:
            metrics.duplicate_tool_call_count += 1
    return metrics


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
