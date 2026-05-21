"""Eval-specific wrappers around runtime run-summary helpers."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from eval.schemas import EvalReport
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
