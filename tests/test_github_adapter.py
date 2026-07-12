"""Tests for pure GitHub advisory conversion."""

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse
from src.integrations.github_adapter import (
    build_github_advisory_payload,
    fingerprint_issue,
)


def _response() -> ReviewResponse:
    return ReviewResponse(
        run_id="run-gh",
        report=ReviewReport(
            summary="summary",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/app.py:10",
                    evidence="+ risky_call()",
                    suggestion="Guard the call.",
                    confidence=0.9,
                ),
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location="src/other.py:5",
                    evidence="+ allow_all = True",
                    suggestion="Restore the authorization guard.",
                    confidence=0.95,
                ),
            ],
        ),
        context=ContextState(),
    )


def test_github_adapter_keeps_inline_comments_on_changed_lines_only() -> None:
    payload = build_github_advisory_payload(_response(), {"src/app.py": {10}})

    assert len(payload.inline_comments) == 1
    assert payload.inline_comments[0].path == "src/app.py"
    assert payload.inline_comments[0].line == 10
    assert len(payload.summary_only_issues) == 1
    assert payload.summary_only_issues[0].reason == "location_not_on_changed_line"
    assert "1 inline candidate" in payload.check_summary


def test_github_adapter_uses_first_changed_line_inside_location_range() -> None:
    response = _response()
    response.report.issues[0].location = "src/app.py:8-12"

    payload = build_github_advisory_payload(response, {"src/app.py": {10, 12}})

    assert len(payload.inline_comments) == 1
    assert payload.inline_comments[0].path == "src/app.py"
    assert payload.inline_comments[0].line == 10
    assert len(payload.summary_only_issues) == 1


def test_fingerprint_is_stable_for_equivalent_issue_text() -> None:
    issue = _response().report.issues[0]
    same = issue.model_copy(
        update={
            "evidence": " +   risky_call() ",
            "suggestion": "Guard   the call.",
        }
    )

    assert fingerprint_issue(issue) == fingerprint_issue(same)


def test_fingerprint_accepts_unparseable_location() -> None:
    issue = _response().report.issues[0].model_copy(
        update={"location": "see discussion thread"}
    )

    assert len(fingerprint_issue(issue)) == 16
