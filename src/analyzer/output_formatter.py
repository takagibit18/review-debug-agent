"""Structured output formatting.

Converts raw analysis results into the canonical actionable format used by
both CLI rendering and (future) API responses.
"""

from __future__ import annotations

from enum import Enum
import re

from pydantic import BaseModel, Field

from src.analyzer.finding_schema import (
    CounterfactualResult,
    EvidenceProvenance,
    FINDING_SCHEMA_VERSION,
    RelatedLocation,
    RepairIntent,
    SourceAnchor,
)


class Severity(str, Enum):
    """Issue severity levels."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    STYLE = "style"


class ReviewIssue(BaseModel):
    """A review finding hypothesis or a verified root-cause finding.

    ``location``, ``evidence`` and ``suggestion`` remain required so existing
    v0.2.2 callers and GitHub publishers keep working.  Structured v0.2.3
    producers additionally populate the causal, repair and provenance fields.
    """

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
    candidate_id: str = Field(
        default="",
        description="Stable semantic-verification identifier for this finding",
    )
    schema_version: str = Field(
        default="1.0",
        description="1.0 for legacy issues; 2.0 for structured hypotheses.",
    )
    finding_id: str = Field(
        default="",
        description="Reviewer-local hypothesis identifier; not a root-cause id.",
    )
    root_cause_id: str = Field(
        default="",
        description="Assigned only after root-cause cluster construction.",
    )
    primary_anchor: SourceAnchor | None = None
    related_locations: list[RelatedLocation] = Field(default_factory=list)
    observed_behavior: str = ""
    causal_mechanism: str = ""
    violated_invariant: str = ""
    repair_intent: RepairIntent = Field(default_factory=RepairIntent)
    trigger: str = ""
    impact: str = ""
    cause_evidence: list[EvidenceProvenance] = Field(default_factory=list)
    contract_evidence: list[EvidenceProvenance] = Field(default_factory=list)
    trigger_evidence: list[EvidenceProvenance] = Field(default_factory=list)
    impact_evidence: list[EvidenceProvenance] = Field(default_factory=list)
    context_manifest_id: str = ""
    member_findings: list[str] = Field(default_factory=list)
    absorbed_roles: dict[str, str] = Field(default_factory=dict)
    counterfactual_result: CounterfactualResult | None = None
    merge_rejection_reasons: list[str] = Field(default_factory=list)

    @property
    def is_structured_hypothesis(self) -> bool:
        return self.schema_version == FINDING_SCHEMA_VERSION

    def all_evidence(self) -> list[EvidenceProvenance]:
        return [
            *self.cause_evidence,
            *self.contract_evidence,
            *self.trigger_evidence,
            *self.impact_evidence,
        ]

    def v022_payload(self) -> dict[str, object]:
        """Return the legacy single-anchor issue contract for old consumers."""

        return {
            "severity": self.severity.value,
            "location": self.location,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "candidate_id": self.candidate_id,
        }


class ReviewReport(BaseModel):
    """Aggregated review output for one run."""

    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")
    schema_version: str = Field(
        default=FINDING_SCHEMA_VERSION,
        description="Version of the review-output compatibility envelope.",
    )

    def v022_payload(self) -> dict[str, object]:
        """Compatibility conversion for integrations pinned to v0.2.2."""

        return {
            "summary": self.summary,
            "issues": [issue.v022_payload() for issue in self.issues],
        }


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
_CODE_EVIDENCE_PATTERN = re.compile(
    r"(?m)^\s*(?:def |class |if |elif |else:|try:|except |return |"
    r"raise |with |using |(?:private|public|protected|internal)\s+|"
    r"[A-Za-z_][\w.]*\s*=|[A-Za-z_][\w.]*\()"
)
_INLINE_CODE_SPAN_PATTERN = re.compile(r"`([^`\n]+)`")
_INLINE_CODE_EVIDENCE_PATTERN = re.compile(
    r"(?:\b(?:private|public|protected|internal|readonly|using|return|if|throw)\b|"
    r"[A-Za-z_][\w.]*\s*\(|[A-Za-z_][\w.]*\s*=|;)"
)


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


def has_specific_code_evidence(evidence: str) -> bool:
    text = evidence.strip()
    if not text:
        return False
    if has_specific_diff_evidence(text):
        return True
    if _CODE_EVIDENCE_PATTERN.search(text):
        return True
    return any(
        _INLINE_CODE_EVIDENCE_PATTERN.search(span) is not None
        for span in _INLINE_CODE_SPAN_PATTERN.findall(text)
    )
