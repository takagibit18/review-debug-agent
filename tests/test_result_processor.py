"""Tests for result processor behavior."""

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.result_processor import ResultProcessor


def test_merge_review_reports_sorts_by_severity_priority() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(severity=Severity.INFO, location="a:1", evidence="x", suggestion="x"),
            ReviewIssue(severity=Severity.WARNING, location="b:1", evidence="x", suggestion="x"),
            ReviewIssue(severity=Severity.STYLE, location="c:1", evidence="x", suggestion="x"),
            ReviewIssue(severity=Severity.CRITICAL, location="d:1", evidence="x", suggestion="x"),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])
    order = [issue.severity for issue in merged.issues]
    assert order == [Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.STYLE]


def test_result_processor_budget_from_constructor() -> None:
    processor = ResultProcessor(token_budget=10)
    assert processor.is_budget_exhausted(9) is False
    assert processor.is_budget_exhausted(10) is True
    assert ContextState() is not None
