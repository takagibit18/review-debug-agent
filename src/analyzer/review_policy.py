"""Shared deterministic policy helpers for review issue filtering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class IssueFilterDecision:
    """Explain one deterministic review-output filter decision."""

    finding_id: str
    passed: bool
    reason_codes: tuple[str, ...]
    severity: Severity
    confidence: float
    standard_threshold: float | None
    relaxed_threshold: float | None
    evidence_specific: bool
    risk_pattern_matched: bool

    def event_payload(self, *, original_index: int) -> dict[str, object]:
        """Return a content-light event payload for one submitted finding."""

        return {
            "finding_id": self.finding_id,
            "original_index": original_index,
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "severity": self.severity.value,
            "confidence": self.confidence,
            "standard_threshold": self.standard_threshold,
            "relaxed_threshold": self.relaxed_threshold,
            "evidence_specific": self.evidence_specific,
            "risk_pattern_matched": self.risk_pattern_matched,
        }


def evaluate_issue_filter(issue: ReviewIssue) -> IssueFilterDecision:
    """Return the current filter verdict plus every contributing reason."""

    evidence_specific = has_specific_code_evidence(issue.evidence)
    combined = f"{issue.evidence}\n{issue.suggestion}"
    risk_pattern_matched = RISK_WARNING_PATTERN.search(combined) is not None
    finding_id = _filter_finding_id(issue)

    if issue.severity == Severity.CRITICAL:
        reasons: list[str] = []
        if issue.confidence < MIN_CRITICAL_CONFIDENCE:
            reasons.append("critical_confidence_below_threshold")
        if not evidence_specific:
            reasons.append("critical_evidence_not_specific")
        passed = not reasons
        if passed:
            reasons.append("critical_policy_passed")
        return IssueFilterDecision(
            finding_id=finding_id,
            passed=passed,
            reason_codes=tuple(reasons),
            severity=issue.severity,
            confidence=issue.confidence,
            standard_threshold=MIN_CRITICAL_CONFIDENCE,
            relaxed_threshold=None,
            evidence_specific=evidence_specific,
            risk_pattern_matched=risk_pattern_matched,
        )

    if issue.severity == Severity.WARNING:
        if issue.confidence >= MIN_WARNING_CONFIDENCE:
            return IssueFilterDecision(
                finding_id=finding_id,
                passed=True,
                reason_codes=("warning_standard_confidence_passed",),
                severity=issue.severity,
                confidence=issue.confidence,
                standard_threshold=MIN_WARNING_CONFIDENCE,
                relaxed_threshold=MIN_RISK_WARNING_CONFIDENCE,
                evidence_specific=evidence_specific,
                risk_pattern_matched=risk_pattern_matched,
            )

        reasons = ["warning_confidence_below_standard_threshold"]
        if issue.confidence < MIN_RISK_WARNING_CONFIDENCE:
            reasons.append("warning_confidence_below_relaxed_threshold")
        if not evidence_specific:
            reasons.append("warning_evidence_not_specific")
        if not risk_pattern_matched:
            reasons.append("warning_risk_pattern_missing")
        passed = (
            issue.confidence >= MIN_RISK_WARNING_CONFIDENCE
            and evidence_specific
            and risk_pattern_matched
        )
        if passed:
            reasons.append("warning_relaxed_risk_policy_passed")
        return IssueFilterDecision(
            finding_id=finding_id,
            passed=passed,
            reason_codes=tuple(reasons),
            severity=issue.severity,
            confidence=issue.confidence,
            standard_threshold=MIN_WARNING_CONFIDENCE,
            relaxed_threshold=MIN_RISK_WARNING_CONFIDENCE,
            evidence_specific=evidence_specific,
            risk_pattern_matched=risk_pattern_matched,
        )

    return IssueFilterDecision(
        finding_id=finding_id,
        passed=True,
        reason_codes=("non_risk_passthrough",),
        severity=issue.severity,
        confidence=issue.confidence,
        standard_threshold=None,
        relaxed_threshold=None,
        evidence_specific=evidence_specific,
        risk_pattern_matched=risk_pattern_matched,
    )


def passes_issue_filter(issue: ReviewIssue) -> bool:
    """Return whether an issue survives the current review output filter."""
    return evaluate_issue_filter(issue).passed


def is_specific_risk_warning(issue: ReviewIssue) -> bool:
    """Return whether a warning qualifies for the relaxed risk threshold."""
    decision = evaluate_issue_filter(issue)
    return bool(
        issue.severity == Severity.WARNING
        and issue.confidence < MIN_WARNING_CONFIDENCE
        and decision.passed
    )


def _filter_finding_id(issue: ReviewIssue) -> str:
    explicit = issue.finding_id.strip() or issue.candidate_id.strip()
    if explicit:
        return explicit
    normalized = "\n".join(
        (
            issue.severity.value,
            issue.location.strip().replace("\\", "/"),
            issue.evidence.strip(),
            issue.suggestion.strip(),
        )
    )
    return "FILTER-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
