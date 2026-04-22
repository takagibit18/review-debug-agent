"""Result formatting and stop-hook checks for phase 4."""

from __future__ import annotations

from uuid import uuid4

from src.analyzer.context_state import ContextState, DecisionStep
from src.analyzer.output_formatter import (
    ReviewIssue,
    ReviewReport,
    Severity,
    has_specific_diff_evidence,
)
from src.analyzer.schemas import AnalysisPlan, DebugResponse, ReviewResponse
from src.tools.base import ToolResult


class ResultProcessor:
    """Convert phase outputs into final structured responses."""
    _MIN_CRITICAL_CONFIDENCE = 0.85
    _MIN_WARNING_CONFIDENCE = 0.85

    def __init__(self, token_budget: int = 12000) -> None:
        self._token_budget = token_budget

    def format_review(
        self,
        plan: AnalysisPlan,
        tool_results: list[ToolResult],
        state: ContextState,
    ) -> tuple[ReviewResponse, bool]:
        blocking_error = any((not result.ok) for result in tool_results)
        report = plan.draft_review or ReviewReport(
            summary="Review pipeline completed with placeholder summary."
        )
        if blocking_error and not report.summary:
            report.summary = "Tool execution failed; returning partial review output."
        report = self.merge_review_reports([report])
        state.decisions.append(
            DecisionStep(
                phase="format",
                action="Build structured review response",
                result="Formatted review response",
            )
        )
        response = ReviewResponse(run_id=str(uuid4()), report=report, context=state)
        return response, blocking_error

    def format_debug(
        self,
        plan: AnalysisPlan,
        tool_results: list[ToolResult],
        state: ContextState,
    ) -> tuple[DebugResponse, bool]:
        blocking_error = any((not result.ok) for result in tool_results)
        response = plan.draft_debug or DebugResponse(
            run_id="",
            summary="Debug pipeline completed with placeholder summary.",
            hypotheses=[],
            steps=[],
            context=state,
        )
        response.run_id = str(uuid4())
        response.context = state
        if blocking_error and not response.summary:
            response.summary = "Tool execution failed; returning partial debug output."
        state.decisions.append(
            DecisionStep(
                phase="format",
                action="Build structured debug response",
                result="Formatted debug response",
            )
        )
        return response, blocking_error

    @staticmethod
    def merge_review_reports(reports: list[ReviewReport]) -> ReviewReport:
        severity_rank = {
            "critical": 0,
            "warning": 1,
            "info": 2,
            "style": 3,
        }
        merged_summary = " ".join([item.summary for item in reports if item.summary]).strip()
        seen: set[tuple[str, str, str]] = set()
        merged_issues: list[ReviewIssue] = []
        for report in reports:
            for issue in report.issues:
                if not ResultProcessor._passes_issue_filter(issue):
                    continue
                key = (issue.severity.value, issue.location, issue.suggestion)
                if key in seen:
                    continue
                seen.add(key)
                merged_issues.append(issue)
        merged_issues.sort(
            key=lambda issue: (
                severity_rank.get(issue.severity.value, 99),
                issue.location,
                issue.suggestion,
            )
        )
        return ReviewReport(summary=merged_summary, issues=merged_issues)

    @staticmethod
    def _passes_issue_filter(issue: ReviewIssue) -> bool:
        if issue.severity == Severity.CRITICAL:
            return (
                issue.confidence >= ResultProcessor._MIN_CRITICAL_CONFIDENCE
                and has_specific_diff_evidence(issue.evidence)
            )
        if issue.severity == Severity.WARNING:
            return (
                issue.confidence >= ResultProcessor._MIN_WARNING_CONFIDENCE
                and has_specific_diff_evidence(issue.evidence)
            )
        return True

    def is_budget_exhausted(self, total_tokens: int) -> bool:
        """Backward-compatible: True when soft cap is reached."""
        return total_tokens >= self._token_budget

    def budget_state(self, total_tokens: int) -> str:
        """Return 'none' | 'soft_capped' | 'hard_capped'."""
        if total_tokens >= 2 * self._token_budget:
            return "hard_capped"
        if total_tokens >= self._token_budget:
            return "soft_capped"
        return "none"
