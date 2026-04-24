"""Render review results into a GitHub PR comment."""

from __future__ import annotations

from src.analyzer.output_formatter import ReviewIssue, triage_review_report
from src.analyzer.schemas import ReviewResponse

PR_REVIEW_COMMENT_MARKER = "<!-- cr-debug-agent:pr-review -->"
_MAX_DETAIL_CHARS = 280


def render_pr_review_comment(
    response: ReviewResponse,
    *,
    repository: str = "",
    pr_number: int | None = None,
    commit_sha: str = "",
    max_issues_per_section: int = 5,
) -> str:
    """Render one structured review response as GitHub-flavored Markdown."""
    triage = triage_review_report(response.report)
    lines = [
        PR_REVIEW_COMMENT_MARKER,
        "## CR Debug Agent Review",
    ]
    if repository and pr_number is not None:
        lines.append(f"Repository: `{repository}`  ")
        lines.append(f"PR: `#{pr_number}`")
    if commit_sha:
        lines.append(f"Commit: `{commit_sha[:12]}`")
    lines.extend(
        [
            "",
            f"Summary: {response.report.summary or 'No summary provided.'}",
            "",
            "| Signal | Value |",
            "| --- | --- |",
            f"| Immediate attention | {'yes' if triage.must_fix_critical else 'no'} |",
            f"| Must-fix critical bugs | {len(triage.must_fix_critical)} |",
            f"| Other bug findings | {len(triage.other_bug_findings)} |",
            f"| Optimization suggestions | {len(triage.optimization_suggestions)} |",
            f"| Tracked files | {len(response.context.current_files)} |",
            f"| Run ID | `{response.run_id}` |",
        ]
    )

    if response.context.errors:
        lines.extend(["", "### Execution Notes"])
        for error in response.context.errors[:3]:
            prefix = f"`{error.category}`"
            target = f" `{error.file}`" if error.file else ""
            lines.append(f"- {prefix}{target}: {_trim(error.message, _MAX_DETAIL_CHARS)}")

    issue_sections = [
        ("Must-Fix Critical Bugs", triage.must_fix_critical),
        ("Other Bug Findings", triage.other_bug_findings),
        ("Optimization Suggestions", triage.optimization_suggestions),
    ]
    rendered_any = False
    for title, issues in issue_sections:
        if not issues:
            continue
        rendered_any = True
        lines.extend(["", f"### {title} ({len(issues)})"])
        for index, issue in enumerate(issues[:max_issues_per_section], start=1):
            lines.extend(_render_issue(issue, index))
        remaining = len(issues) - max_issues_per_section
        if remaining > 0:
            lines.append(f"- ... and {remaining} more findings in this section.")

    if not rendered_any:
        lines.extend(
            [
                "",
                "### Result",
                "No review issues were flagged for this PR.",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _render_issue(issue: ReviewIssue, index: int) -> list[str]:
    lines = [
        f"{index}. `{issue.location}` | severity `{issue.severity.value}` | confidence `{issue.confidence:.2f}`",
        f"   Evidence: {_trim(_single_line(issue.evidence), _MAX_DETAIL_CHARS)}",
        f"   Suggested fix: {_trim(_single_line(issue.suggestion), _MAX_DETAIL_CHARS)}",
    ]
    return lines


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _trim(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
