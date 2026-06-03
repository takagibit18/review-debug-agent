"""Read-only deterministic validator for candidate review drafts."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, Severity, has_specific_code_evidence
from src.analyzer.review_policy import (
    MIN_CRITICAL_CONFIDENCE,
    MIN_RISK_WARNING_CONFIDENCE,
    MIN_WARNING_CONFIDENCE,
    passes_issue_filter,
)
from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.review_context import ReviewToolContext


_SUMMARY_RISK_PATTERN = re.compile(
    r"\b(bug|regression|breaking|breaks?|compatibility|user-visible|risk)\b",
    re.IGNORECASE,
)


class ReviewDraftIssueInput(BaseModel):
    """Candidate review issue submitted by the model for policy feedback."""

    severity: Severity
    location: str
    evidence: str
    suggestion: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ValidateReviewDraftInput(BaseModel):
    """Validated input for review draft validation."""

    summary: str = Field(default="", description="Candidate review summary")
    issues: list[ReviewDraftIssueInput] = Field(
        default_factory=list,
        description="Candidate issues to validate before submit_review",
    )


class ValidateReviewDraftTool(BaseTool):
    """Provide deterministic policy feedback for a candidate ReviewReport."""

    def __init__(self, review_context: ReviewToolContext) -> None:
        self._context = review_context

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="validate_review_draft",
            description=(
                "Validate a candidate review draft against deterministic output policy: "
                "canonical locations, changed-line targeting, evidence specificity, and "
                "the current critical/warning filter. This gives policy feedback only; "
                "it does not replace submit_review and does not judge semantic correctness."
            ),
            parameters=ValidateReviewDraftInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return deterministic validation feedback for a candidate draft."""
        data = ValidateReviewDraftInput(**kwargs)
        issue_results = [
            self._validate_issue(index, issue)
            for index, issue in enumerate(data.issues)
        ]
        effective_issue_count = sum(
            1 for item in issue_results if item["passes_current_filter"]
        )
        summary = data.summary.strip()
        summary_warnings: list[str] = []
        risky_summary = _SUMMARY_RISK_PATTERN.search(summary) is not None
        if risky_summary and effective_issue_count == 0:
            summary_warnings.append(
                "summary mentions bug/regression/breaking/user-visible risk but no issue passes current filter"
            )
        return {
            "normalized_summary": summary,
            "issue_results": issue_results,
            "summary_warnings": summary_warnings,
            "effective_issue_count": effective_issue_count,
            "should_submit_empty_issues": effective_issue_count == 0 and not summary_warnings,
        }

    def _validate_issue(self, index: int, input_issue: ReviewDraftIssueInput) -> dict[str, Any]:
        issue = ReviewIssue(
            severity=input_issue.severity,
            location=input_issue.location,
            evidence=input_issue.evidence,
            suggestion=input_issue.suggestion,
            confidence=input_issue.confidence,
        )
        location = normalize_location(issue.location)
        evidence_specific = has_specific_code_evidence(issue.evidence)
        passes_filter = passes_issue_filter(issue)
        location_on_changed_line = self._location_on_changed_line(
            location.path,
            location.line,
            location.end_line,
        )
        fail_reasons: list[str] = []
        repair_hints: list[str] = []

        if not location.valid:
            fail_reasons.append(location.warning or "invalid_location")
            repair_hints.append("use canonical path[:line[-end_line]] location")
        elif location.warning:
            repair_hints.append(f"use canonical location {location.canonical}")

        if issue.severity in {Severity.CRITICAL, Severity.WARNING} and not location_on_changed_line:
            repair_hints.append("move location to a changed line")

        if issue.severity == Severity.CRITICAL:
            if issue.confidence < MIN_CRITICAL_CONFIDENCE:
                fail_reasons.append("confidence_below_critical_threshold")
                repair_hints.append("increase confidence to at least 0.85")
            if not evidence_specific:
                fail_reasons.append("evidence_not_specific")
                repair_hints.append("add concrete diff or code evidence")
        elif issue.severity == Severity.WARNING and not passes_filter:
            if issue.confidence < MIN_RISK_WARNING_CONFIDENCE:
                fail_reasons.append("confidence_below_warning_threshold")
                repair_hints.append("increase confidence to at least 0.85")
            elif issue.confidence < MIN_WARNING_CONFIDENCE:
                fail_reasons.append("warning_lacks_specific_risk_evidence")
                repair_hints.append("add concrete diff evidence and describe the user-visible risk")
            if not evidence_specific:
                fail_reasons.append("evidence_not_specific")
                repair_hints.append("add concrete diff or code evidence")
            repair_hints.append("lower severity to info/style or remove issue if evidence is speculative")

        return {
            "original_index": index,
            "normalized_location": location.canonical,
            "severity": issue.severity.value,
            "confidence": issue.confidence,
            "location_valid": location.valid,
            "location_on_changed_line": location_on_changed_line,
            "evidence_specific": evidence_specific,
            "passes_current_filter": passes_filter,
            "fail_reasons": list(dict.fromkeys(fail_reasons)),
            "repair_hints": list(dict.fromkeys(repair_hints)),
        }

    def _location_on_changed_line(
        self,
        path: str | None,
        line: int | None,
        end_line: int | None,
    ) -> bool:
        if path is None or line is None:
            return False
        changed_lines = self._context.changed_lines_by_file.get(path, set())
        if not changed_lines:
            return False
        last_line = end_line or line
        return any(candidate in changed_lines for candidate in range(line, last_line + 1))
