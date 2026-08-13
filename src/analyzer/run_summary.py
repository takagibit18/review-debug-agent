"""Runtime run summaries derived from analyzer event logs and artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.analyzer.finding_funnel import FindingFunnel

EventLogStatus = Literal["ok", "missing", "parse_error"]
PublishStatus = Literal["not_requested", "dry_run", "published", "failed"]


class RunSummary(BaseModel):
    """Compact diagnostic view of one event log."""

    run_id: str = ""
    event_log_path: str = ""
    event_log_status: EventLogStatus = "ok"
    parse_error: str = ""
    finish_reasons: list[str] = Field(default_factory=list)
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
    publish_status: PublishStatus = "not_requested"
    finding_verifier_mode: str = "off"
    model_raw_issue_count: int = 0
    verifier_candidate_count: int = 0
    finding_candidate_count: int = 0
    finding_accepted_count: int = 0
    finding_rejected_count: int = 0
    finding_needs_evidence_count: int = 0
    finding_downgraded_count: int = 0
    finding_reason_codes: dict[str, int] = Field(default_factory=dict)
    raw_verifier_accepted_count: int = 0
    raw_verifier_rejected_count: int = 0
    raw_verifier_needs_evidence_count: int = 0
    raw_verifier_downgraded_count: int = 0
    raw_finding_reason_codes: dict[str, int] = Field(default_factory=dict)
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
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            summary.total_tokens += int(usage["total_tokens"])
        tool_calls = payload.get("tool_call_summaries")
        if isinstance(tool_calls, list):
            summary.tool_call_count += len(tool_calls)

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

    if event_type == "phase_end" and phase == "review_complete":
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
        summary.finding_verifier_mode = str(payload.get("mode", "off") or "off")
        summary.model_raw_issue_count = int(
            payload.get("model_raw_issue_count", summary.model_raw_issue_count) or 0
        )
        summary.verifier_candidate_count = int(
            payload.get("verifier_candidate_count", payload.get("candidate_count", 0))
            or 0
        )
        summary.finding_candidate_count = int(payload.get("candidate_count", 0) or 0)

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
        summary.finding_needs_evidence_count = int(
            payload.get("needs_evidence_count", 0) or 0
        )
        summary.finding_downgraded_count = int(payload.get("downgraded_count", 0) or 0)
        summary.raw_verifier_accepted_count = int(
            payload.get("raw_accepted_count", summary.finding_accepted_count) or 0
        )
        summary.raw_verifier_rejected_count = int(
            payload.get("raw_rejected_count", summary.finding_rejected_count) or 0
        )
        summary.raw_verifier_needs_evidence_count = int(
            payload.get(
                "raw_needs_evidence_count", summary.finding_needs_evidence_count
            )
            or 0
        )
        summary.raw_verifier_downgraded_count = int(
            payload.get("raw_downgraded_count", summary.finding_downgraded_count) or 0
        )
        summary.deterministic_evidence_checked_count = int(
            payload.get("deterministic_evidence_checked_count", 0) or 0
        )
        summary.deterministic_evidence_passed_count = int(
            payload.get("deterministic_evidence_passed_count", 0) or 0
        )
        summary.deterministic_evidence_rejected_count = int(
            payload.get("deterministic_evidence_rejected_count", 0) or 0
        )
        reason_codes = payload.get("reason_codes", [])
        if isinstance(reason_codes, list):
            for raw_code in reason_codes:
                code = str(raw_code).strip()
                if code:
                    summary.finding_reason_codes[code] = (
                        summary.finding_reason_codes.get(code, 0) + 1
                    )
        raw_reason_codes = payload.get("raw_reason_codes", reason_codes)
        if isinstance(raw_reason_codes, list):
            for raw_code in raw_reason_codes:
                code = str(raw_code).strip()
                if code:
                    summary.raw_finding_reason_codes[code] = (
                        summary.raw_finding_reason_codes.get(code, 0) + 1
                    )

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
