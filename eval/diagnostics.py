"""Eval diagnostics built from reports and event logs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from eval.run_summary import RunSummary, summarize_event_log
from eval.schemas import EvalReport, EvalResult

DiagnosticReason = Literal[
    "hit",
    "miss_no_issue",
    "miss_filtered_issue",
    "schema_invalid",
    "placeholder",
    "budget_capped",
    "submit_invalid",
    "fp_negative",
    "event_log_missing",
    "event_log_parse_error",
    "workflow_invalid",
]


class EvalFixtureDiagnosis(BaseModel):
    """Diagnostic classification for one fixture result."""

    fixture_id: str
    reasons: list[DiagnosticReason] = Field(default_factory=list)
    matched_count: int = 0
    expected_count: int = 0
    actual_count: int = 0
    false_positive_count: int = 0
    budget_state: str = "none"
    run_summary: RunSummary = Field(default_factory=RunSummary)
    note: str = ""


class EvalDiagnosticsReport(BaseModel):
    """Suite-level diagnostics view."""

    suite: str
    generated_at: str
    fixtures: list[EvalFixtureDiagnosis] = Field(default_factory=list)


def build_eval_diagnostics(report: EvalReport) -> EvalDiagnosticsReport:
    return EvalDiagnosticsReport(
        suite=report.suite,
        generated_at=report.generated_at,
        fixtures=[diagnose_result(item) for item in report.results],
    )


def diagnose_result(result: EvalResult) -> EvalFixtureDiagnosis:
    run_summary = summarize_event_log(result.event_log_path)
    reasons: list[DiagnosticReason] = []

    if not result.schema_valid:
        reasons.append("schema_invalid")
    if result.placeholder_summary:
        reasons.append("placeholder")
    if result.budget_state in {"soft_capped", "hard_capped"}:
        reasons.append("budget_capped")
    if run_summary.submit_validation_errors:
        reasons.append("submit_invalid")
    if run_summary.event_log_status == "missing":
        reasons.append("event_log_missing")
    if run_summary.event_log_status == "parse_error":
        reasons.append("event_log_parse_error")
    if result.workflow_invalid or run_summary.workflow_invalid:
        reasons.append("workflow_invalid")

    if result.expected_count == 0 and result.false_positive_count > 0:
        reasons.append("fp_negative")
    elif result.expected_count > 0 and result.matched_count >= result.expected_count:
        reasons.append("hit")
    elif result.expected_count > 0:
        if _has_raw_issues(result) or result.actual_count > 0:
            reasons.append("miss_filtered_issue")
        else:
            reasons.append("miss_no_issue")
    elif not reasons:
        reasons.append("hit")

    return EvalFixtureDiagnosis(
        fixture_id=result.fixture_id,
        reasons=_dedupe(reasons),
        matched_count=result.matched_count,
        expected_count=result.expected_count,
        actual_count=result.actual_count,
        false_positive_count=result.false_positive_count,
        budget_state=result.budget_state,
        run_summary=run_summary,
        note=_build_note(result, reasons),
    )


def load_diagnostics_report(path: str | Path) -> EvalDiagnosticsReport:
    report = EvalReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    return build_eval_diagnostics(report)


def _has_raw_issues(result: EvalResult) -> bool:
    raw = result.raw_output or {}
    report = raw.get("report")
    if isinstance(report, dict):
        issues = report.get("issues")
        return isinstance(issues, list) and bool(issues)
    issues = raw.get("issues")
    return isinstance(issues, list) and bool(issues)


def _dedupe(reasons: list[DiagnosticReason]) -> list[DiagnosticReason]:
    seen: set[DiagnosticReason] = set()
    out: list[DiagnosticReason] = []
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        out.append(reason)
    return out


def _build_note(result: EvalResult, reasons: list[DiagnosticReason]) -> str:
    if "hit" in reasons:
        return "Expected issue matched."
    if "fp_negative" in reasons:
        return "Negative fixture produced effective issues."
    if "workflow_invalid" in reasons:
        missing = result.workflow_missing_steps
        suffix = f": {', '.join(missing)}" if missing else ""
        return "Required review workflow was incomplete" + suffix + "."
    if "miss_filtered_issue" in reasons:
        return "Model produced issues, but none counted as a matched effective issue."
    if "miss_no_issue" in reasons:
        return "Model did not produce a countable issue for this positive fixture."
    if "placeholder" in reasons:
        return "Pipeline returned placeholder output."
    if "schema_invalid" in reasons:
        return "Output did not satisfy eval schema validity."
    return ""
