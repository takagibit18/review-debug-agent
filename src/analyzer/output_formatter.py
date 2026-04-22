"""Structured output formatting.

Converts raw analysis results into the canonical actionable format used by
both CLI rendering and (future) API responses.
"""

from __future__ import annotations

from enum import Enum
import re

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Issue severity levels."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    STYLE = "style"


class ReviewIssue(BaseModel):
    """A single review finding."""

    severity: Severity
    location: str = Field(
        ...,
        description="Canonical location: path[:line[-end_line]] using repo-relative forward-slash paths",
    )
    evidence: str = Field(..., description="Code snippet or observation")
    suggestion: str = Field(..., description="Recommended fix or action")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Model confidence"
    )


class ReviewReport(BaseModel):
    """Aggregated review output for one run."""

    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")


class ReviewTriage(BaseModel):
    """Second-pass buckets used to separate bugs from optimizations."""

    must_fix_critical: list[ReviewIssue] = Field(
        default_factory=list,
        description="High-confidence critical findings backed by explicit diff evidence.",
    )
    other_bug_findings: list[ReviewIssue] = Field(
        default_factory=list,
        description="Bug or regression findings that do not meet the strict must-fix gate.",
    )
    optimization_suggestions: list[ReviewIssue] = Field(
        default_factory=list,
        description="Non-blocking optimization, readability, or style suggestions.",
    )


_MUST_FIX_MIN_CONFIDENCE = 0.85
_DIFF_HEADER_PATTERN = re.compile(r"(?m)^diff --git ")
_DIFF_HUNK_PATTERN = re.compile(r"(?m)^@@ .+ @@")
_DIFF_CHANGE_LINE_PATTERN = re.compile(r"(?m)^(?:\+|-)(?!\+\+|--).+\S")


def triage_review_report(report: ReviewReport) -> ReviewTriage:
    """Split review findings into must-fix bugs, other bugs, and optimizations."""

    must_fix_critical: list[ReviewIssue] = []
    other_bug_findings: list[ReviewIssue] = []
    optimization_suggestions: list[ReviewIssue] = []
    for issue in report.issues:
        if _is_must_fix_critical(issue):
            must_fix_critical.append(issue)
            continue
        if issue.severity in {Severity.INFO, Severity.STYLE}:
            optimization_suggestions.append(issue)
            continue
        other_bug_findings.append(issue)
    return ReviewTriage(
        must_fix_critical=must_fix_critical,
        other_bug_findings=other_bug_findings,
        optimization_suggestions=optimization_suggestions,
    )


def _is_must_fix_critical(issue: ReviewIssue) -> bool:
    return (
        issue.severity == Severity.CRITICAL
        and issue.confidence >= _MUST_FIX_MIN_CONFIDENCE
        and has_specific_diff_evidence(issue.evidence)
    )


def has_specific_diff_evidence(evidence: str) -> bool:
    text = evidence.strip()
    if not text:
        return False
    if "```diff" in text:
        return True
    if _DIFF_HEADER_PATTERN.search(text):
        return True
    if _DIFF_HUNK_PATTERN.search(text):
        return True
    changed_lines = _DIFF_CHANGE_LINE_PATTERN.findall(text)
    return any(len(line.strip()) > 4 for line in changed_lines)
