"""Read-only deterministic validator for candidate review drafts."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from src.analyzer.finding_schema import EvidenceProvenance
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import (
    ReviewIssue,
    Severity,
    has_specific_code_evidence,
)
from src.analyzer.review_policy import (
    MIN_CRITICAL_CONFIDENCE,
    MIN_RISK_WARNING_CONFIDENCE,
    MIN_WARNING_CONFIDENCE,
    evaluate_issue_filter,
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
    cause_evidence: list[EvidenceProvenance] = Field(
        default_factory=list,
        description=(
            "Role-specific causal evidence; risk findings need at least one entry "
            "whose location intersects code changed by this PR."
        ),
    )


class ValidateReviewDraftInput(BaseModel):
    """Validated input for review draft validation."""

    summary: str = Field(
        default="",
        description="Your candidate review summary text; the tool checks whether it "
        "mentions bug, regression, or breaking-change keywords without a corresponding "
        "issue that passes the output filter",
    )
    issues: list[ReviewDraftIssueInput] = Field(
        default_factory=list,
        description="The list of candidate issues you plan to include in submit_review; "
        "pass every issue here first so the tool can validate each one before submission",
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
                "Check your candidate review issues against output policy before calling "
                "submit_review. For each issue, this tool verifies: whether the location "
                "uses the correct canonical file path format, whether warning/critical "
                "cause_evidence contains a PR causal anchor on changed code, whether the "
                "evidence contains concrete code or diff snippets, and whether confidence "
                "meets the threshold "
                "for the chosen severity level. Use this BEFORE submit_review — it gives "
                "you deterministic policy feedback so you can fix location formatting "
                "errors, gather missing evidence, recalibrate severity/confidence, and drop "
                "issues that would not pass the output filter. Never raise confidence merely "
                "to cross a threshold. This tool only validates policy compliance; "
                "it does not judge whether your analysis is semantically correct and it "
                "does not replace submit_review."
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
            "should_submit_empty_issues": effective_issue_count == 0
            and not summary_warnings,
        }

    def _validate_issue(
        self, index: int, input_issue: ReviewDraftIssueInput
    ) -> dict[str, Any]:
        issue = ReviewIssue(
            severity=input_issue.severity,
            location=input_issue.location,
            evidence=input_issue.evidence,
            suggestion=input_issue.suggestion,
            confidence=input_issue.confidence,
        )
        location = normalize_location(issue.location)
        evidence_specific = has_specific_code_evidence(issue.evidence)
        filter_decision = evaluate_issue_filter(issue)
        passes_output_filter = filter_decision.passed
        location_on_changed_line = self._location_on_changed_line(
            location.path,
            location.line,
            location.end_line,
        )
        pr_causal_anchor_on_changed_line = any(
            self._location_on_changed_line(
                evidence.file,
                evidence.line,
                evidence.end_line,
            )
            for evidence in input_issue.cause_evidence
        )
        causality_required = issue.severity in {
            Severity.CRITICAL,
            Severity.WARNING,
        }
        passes_causality = not causality_required or pr_causal_anchor_on_changed_line
        passes_filter = passes_output_filter and passes_causality
        fail_reasons: list[str] = []
        repair_hints: list[str] = []

        if not location.valid:
            fail_reasons.append(location.warning or "invalid_location")
            repair_hints.append("use canonical path[:line[-end_line]] location")
        elif location.warning:
            repair_hints.append(f"use canonical location {location.canonical}")

        if causality_required and not pr_causal_anchor_on_changed_line:
            fail_reasons.append("pr_causal_anchor_missing")
            repair_hints.append(
                "cite at least one real changed line in cause_evidence; keep location at "
                "the clearest issue display site"
            )

        if issue.severity == Severity.CRITICAL:
            if issue.confidence < MIN_CRITICAL_CONFIDENCE:
                fail_reasons.append("confidence_below_critical_threshold")
                repair_hints.append(
                    "gather stronger evidence or lower severity; do not inflate confidence "
                    "to cross 0.85"
                )
            if not evidence_specific:
                fail_reasons.append("evidence_not_specific")
                repair_hints.append("add concrete diff or code evidence")
        elif issue.severity == Severity.WARNING and not passes_output_filter:
            if issue.confidence < MIN_RISK_WARNING_CONFIDENCE:
                fail_reasons.append("confidence_below_warning_threshold")
                repair_hints.append(
                    "gather stronger evidence or keep the concern non-risk; do not inflate "
                    "confidence"
                )
            elif issue.confidence < MIN_WARNING_CONFIDENCE:
                fail_reasons.append("warning_lacks_specific_risk_evidence")
                repair_hints.append(
                    "add concrete diff evidence and describe the user-visible risk"
                )
            if not evidence_specific:
                fail_reasons.append("evidence_not_specific")
                repair_hints.append("add concrete diff or code evidence")
            repair_hints.append(
                "lower severity to info/style or remove issue if evidence is speculative"
            )

        return {
            "original_index": index,
            "normalized_location": location.canonical,
            "severity": issue.severity.value,
            "confidence": issue.confidence,
            "location_valid": location.valid,
            "location_on_changed_line": location_on_changed_line,
            "pr_causal_anchor_on_changed_line": pr_causal_anchor_on_changed_line,
            "evidence_specific": evidence_specific,
            "passes_current_filter": passes_filter,
            "filter_reason_codes": list(filter_decision.reason_codes),
            "standard_threshold": filter_decision.standard_threshold,
            "relaxed_threshold": filter_decision.relaxed_threshold,
            "risk_pattern_matched": filter_decision.risk_pattern_matched,
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
        return any(
            candidate in changed_lines for candidate in range(line, last_line + 1)
        )
