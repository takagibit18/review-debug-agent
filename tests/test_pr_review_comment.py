"""Tests for PR review comment rendering."""

from __future__ import annotations

from src.analyzer.context_state import ContextState, ErrorDetail
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse
from src.reporting.pr_review_comment import (
    PR_REVIEW_COMMENT_MARKER,
    render_pr_review_comment,
)


def test_render_pr_review_comment_renders_triaged_sections() -> None:
    response = ReviewResponse(
        run_id="run-pr-comment",
        report=ReviewReport(
            summary="Found one critical regression and one informational note.",
            issues=[
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location="src/auth.py:12",
                    evidence="@@ -12,2 +12,2 @@\n- return is_admin(user)\n+ return True",
                    suggestion="Restore the permission check.",
                    confidence=0.96,
                ),
                ReviewIssue(
                    severity=Severity.INFO,
                    location="src/logging.py:8",
                    evidence="logger.debug(payload)",
                    suggestion="Consider reducing verbose logging in production paths.",
                    confidence=0.65,
                ),
            ],
        ),
        context=ContextState(
            current_files=[".", "src/auth.py", "src/logging.py"],
            errors=[
                ErrorDetail(
                    message="Tool execution failed for list_dir: path missing",
                    category="runtime",
                )
            ],
        ),
    )

    markdown = render_pr_review_comment(
        response,
        repository="owner/repo",
        pr_number=17,
        commit_sha="1234567890abcdef",
    )

    assert PR_REVIEW_COMMENT_MARKER in markdown
    assert "## CR Debug Agent Review" in markdown
    assert "Repository: `owner/repo`" in markdown
    assert "PR: `#17`" in markdown
    assert "Commit: `1234567890ab`" in markdown
    assert "### Must-Fix Critical Bugs (1)" in markdown
    assert "### Optimization Suggestions (1)" in markdown
    assert "### Execution Notes" in markdown
    assert "src/auth.py:12" in markdown
    assert "Restore the permission check." in markdown


def test_render_pr_review_comment_handles_empty_findings() -> None:
    response = ReviewResponse(
        run_id="run-pr-empty",
        report=ReviewReport(summary="No obvious issues found.", issues=[]),
        context=ContextState(current_files=["."]),
    )

    markdown = render_pr_review_comment(response)

    assert "### Result" in markdown
    assert "No review issues were flagged for this PR." in markdown
