"""Pure conversion helpers for future GitHub advisory integrations."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, Field

from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, Severity
from src.analyzer.schemas import ReviewResponse


class InlineCommentCandidate(BaseModel):
    """A GitHub inline comment candidate that has not been published."""

    path: str
    line: int
    severity: Severity
    body: str
    fingerprint: str


class SummaryOnlyIssue(BaseModel):
    """Issue that should stay in the check summary rather than inline comments."""

    issue: ReviewIssue
    reason: str
    fingerprint: str


class GitHubAdvisoryPayload(BaseModel):
    """Pure local payload for future GitHub checks/comments."""

    run_id: str
    check_summary: str
    inline_comments: list[InlineCommentCandidate] = Field(default_factory=list)
    summary_only_issues: list[SummaryOnlyIssue] = Field(default_factory=list)
    fingerprints: list[str] = Field(default_factory=list)


def build_github_advisory_payload(
    response: ReviewResponse,
    changed_lines: Mapping[str, Iterable[int]],
) -> GitHubAdvisoryPayload:
    """Convert a ReviewResponse into advisory check data without network calls."""
    normalized_changed = _normalize_changed_lines(changed_lines)
    inline_comments: list[InlineCommentCandidate] = []
    summary_only: list[SummaryOnlyIssue] = []

    for issue in response.report.issues:
        fingerprint = fingerprint_issue(issue)
        parsed = normalize_location(issue.location)
        inline_line = _inline_line_for_location(
            parsed.path if parsed.valid else None,
            parsed.line if parsed.valid else None,
            parsed.end_line if parsed.valid else None,
            normalized_changed,
        )
        if parsed.valid and parsed.path and inline_line is not None:
            inline_comments.append(
                InlineCommentCandidate(
                    path=parsed.path,
                    line=inline_line,
                    severity=issue.severity,
                    body=_comment_body(issue),
                    fingerprint=fingerprint,
                )
            )
            continue
        summary_only.append(
            SummaryOnlyIssue(
                issue=issue,
                reason="location_not_on_changed_line",
                fingerprint=fingerprint,
            )
        )

    fingerprints = [item.fingerprint for item in inline_comments] + [
        item.fingerprint for item in summary_only
    ]
    return GitHubAdvisoryPayload(
        run_id=response.run_id,
        check_summary=_check_summary(response, inline_comments, summary_only),
        inline_comments=inline_comments,
        summary_only_issues=summary_only,
        fingerprints=fingerprints,
    )


def fingerprint_issue(issue: ReviewIssue) -> str:
    """Stable fingerprint for comment lifecycle dedupe/update decisions."""
    parsed = normalize_location(issue.location)
    path = (
        parsed.path
        if parsed.valid and parsed.path is not None
        else issue.location.replace("\\", "/")
    )
    line = str(parsed.line or "")
    parts = [
        path.strip().lower(),
        line,
        issue.severity.value,
        _normalize_text(issue.evidence),
        _normalize_text(issue.suggestion),
    ]
    if issue.root_cause_id:
        parts.append(issue.root_cause_id)
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_changed_lines(
    changed_lines: Mapping[str, Iterable[int]],
) -> dict[str, set[int]]:
    normalized: dict[str, set[int]] = {}
    for path, lines in changed_lines.items():
        normalized[str(path).replace("\\", "/")] = {int(line) for line in lines}
    return normalized


def _inline_line_for_location(
    path: str | None,
    line: int | None,
    end_line: int | None,
    changed_lines: Mapping[str, set[int]],
) -> int | None:
    if not path or line is None:
        return None
    candidates = changed_lines.get(path, set())
    if line in candidates:
        return line
    if end_line is None:
        return None
    for changed_line in sorted(candidates):
        if line <= changed_line <= end_line:
            return changed_line
    return None


def _comment_body(issue: ReviewIssue) -> str:
    body = (
        f"**{issue.severity.value.upper()}** confidence={issue.confidence:.2f}\n\n"
        f"Evidence: {issue.evidence}\n\nSuggestion: {issue.suggestion}"
    )
    if issue.root_cause_id:
        body += f"\n\nRoot cause: `{issue.root_cause_id}`"
    if issue.causal_mechanism:
        body += f"\n\nCausal mechanism: {issue.causal_mechanism}"
    if issue.violated_invariant:
        body += f"\n\nViolated invariant: {issue.violated_invariant}"
    if issue.related_locations:
        body += "\n\nRelated locations: " + ", ".join(
            f"`{location.location}` ({location.role})"
            for location in issue.related_locations
        )
    return body


def _check_summary(
    response: ReviewResponse,
    inline_comments: list[InlineCommentCandidate],
    summary_only: list[SummaryOnlyIssue],
) -> str:
    issues = response.report.issues
    counts = {severity.value: 0 for severity in Severity}
    for issue in issues:
        counts[issue.severity.value] += 1
    return (
        f"Run `{response.run_id}` produced {len(issues)} issue(s): "
        f"{counts['critical']} critical, {counts['warning']} warning, "
        f"{counts['info']} info, {counts['style']} style. "
        f"{len(inline_comments)} inline candidate(s), "
        f"{len(summary_only)} summary-only issue(s)."
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().split()).lower()
