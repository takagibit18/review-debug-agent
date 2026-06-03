"""Shared deterministic policy helpers for review issue filtering."""

from __future__ import annotations

import re

from src.analyzer.output_formatter import ReviewIssue, Severity, has_specific_code_evidence

MIN_CRITICAL_CONFIDENCE = 0.85
MIN_WARNING_CONFIDENCE = 0.85
MIN_RISK_WARNING_CONFIDENCE = 0.70

RISK_WARNING_PATTERN = re.compile(
    r"\b("
    r"NullReferenceException|exception|regression|breaking|behavior(?:al)? change|"
    r"user-visible|compatibility(?: risk)?|incorrect|error|fail(?:ure)?|"
    r"breaks?|throws?|crash|silent(?:ly)?|truncat(?:e|es|ed|ing|ion)|"
    r"replac(?:e|es|ed|ing)"
    r")\b",
    re.IGNORECASE,
)


def passes_issue_filter(issue: ReviewIssue) -> bool:
    """Return whether an issue survives the current review output filter."""
    if issue.severity == Severity.CRITICAL:
        return (
            issue.confidence >= MIN_CRITICAL_CONFIDENCE
            and has_specific_code_evidence(issue.evidence)
        )
    if issue.severity == Severity.WARNING:
        return (
            issue.confidence >= MIN_WARNING_CONFIDENCE
            or is_specific_risk_warning(issue)
        )
    return True


def is_specific_risk_warning(issue: ReviewIssue) -> bool:
    """Return whether a warning qualifies for the relaxed risk threshold."""
    if issue.confidence < MIN_RISK_WARNING_CONFIDENCE:
        return False
    combined = f"{issue.evidence}\n{issue.suggestion}"
    return (
        has_specific_code_evidence(issue.evidence)
        and RISK_WARNING_PATTERN.search(combined) is not None
    )
