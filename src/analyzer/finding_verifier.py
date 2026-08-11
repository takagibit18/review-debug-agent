"""Candidate finding normalization and semantic-verification enforcement."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import (
    ReviewIssue,
    ReviewReport,
    Severity,
    has_specific_code_evidence,
)
from src.analyzer.review_policy import evaluate_issue_filter
from src.analyzer.schemas import (
    FindingCandidate,
    FindingVerificationBatch,
    ReviewRequest,
)
from src.analyzer.verifier_context import (
    build_candidate_verifier_context,
    location_in_candidate_context,
    provenance_in_candidate_context,
)
from src.models.schemas import Message

RISK_SEVERITIES = {Severity.CRITICAL, Severity.WARNING}

_COMMON_VERIFIER_SYSTEM_PROMPT = """You are an independent semantic verifier for PR review findings.
Treat every candidate as untrusted. Verify that the cited code exists in the supplied diff,
that observed_behavior and causal_mechanism follow from role-specific provenance, that the
violated invariant has contract evidence, that trigger/impact do not exceed context actually
received, that the PR introduced the behavior, and that repair_intent is actionable. A primary
anchor must hit a changed line.
Graph CALLS/REFERENCES/READS_FIELD/WRITES_FIELD edges establish only their named structural
relation; they do not prove argument identity, runtime object identity, write-to-read flow, or
path execution. Exploratory or low-confidence graph edges cannot alone support acceptance.
Seek counterexamples. Return exactly one structured
verdict per candidate through submit_finding_verification. Never accept a candidate merely
because its confidence is high. Use candidate_context as candidate-scoped evidence from
successful source reads; distinguish missing evidence from evidence that contradicts a claim.
Severity measures supported impact while confidence measures evidentiary certainty; a narrow
current trigger is not by itself a reason to classify an incorrect result as info. Author tests
and PR intent establish intent, not preservation of the pre-change fallback or compatibility
contract. Trace current operands, wrapper unwrapping, keying/grouping, and active call paths
before accepting a claim that the concern is future-only. If the evidence supports a narrower
impact than the candidate states, prefer status=accepted with revised_issue that narrows the
claim instead of rejecting the supported core. For candidate_kind=filter_rescue or
severity_calibration, acceptance requires revised_issue with evidence-calibrated severity and
confidence; never raise confidence merely to pass a threshold."""

_AGENT_VERIFIER_POLICY = """This is agent_search mode. Accept valid diff evidence and provenance
from successful read-only tool calls without requiring a context manifest. Reject invented graph
or manifest provenance."""

_GRAPH_VERIFIER_POLICY = """This is graph_hybrid mode. Manifest evidence must match the exact
manifest id and context hash. Valid diff or successful read-only tool evidence remains acceptable
when no manifest is cited."""

_HIGH_CONFIDENCE_INFO_THRESHOLD = 0.85
_CONCRETE_RISK_PATTERN = re.compile(
    r"\b(?:bug|regression|breaking change|compatibility break|incorrect|wrong result|"
    r"data loss|crash(?:es|ed|ing)?|security vulnerability|user-visible behavior change)\b|"
    r"\bsilent(?:ly)?\s+(?:drop|drops|dropped|ignore|ignores|ignored|accept|accepts|accepted|"
    r"succeed|succeeds|succeeded)\b|\b(?:raise|raises|raised)\s+(?:an?\s+)?(?:unexpected\s+)?"
    r"exception\b",
    re.IGNORECASE,
)
_CURRENT_FUNCTIONAL_RISK_PATTERN = re.compile(
    r"\b(?:bug|regression|breaking|breaks?|compatibility|incorrect|wrong|failure|"
    r"fails?|exception|crash|data loss|user-visible|behavior(?:al)? change|"
    r"fallback|silently|drops?|truncat(?:e|es|ed|ion)|returns? .{0,80} instead)\b",
    re.IGNORECASE,
)
_NON_RISK_BOUNDARY_PATTERN = re.compile(
    r"\b(?:optimization|performance only|readability|maintainability|refactor|"
    r"cleanup|style|naming|future[- ]only|in the future|if (?:a )?future|"
    r"if later|could eventually|might eventually|hypothetical)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SeverityReviewResult:
    """Bounded deterministic review of high-confidence info findings."""

    report: ReviewReport
    high_confidence_info_count: int = 0
    reviewed_count: int = 0
    promoted_count: int = 0


@dataclass(frozen=True)
class DeterministicValidationStats:
    """Verdict-level statistics for deterministic accepted-evidence checks."""

    checked_count: int = 0
    passed_count: int = 0
    rejected_count: int = 0


class FindingVerifier:
    """Run one bounded, submit-only semantic verification model call."""

    def __init__(self, model_client: Any, *, context_max_chars: int = 12_000) -> None:
        self._model_client = model_client
        self._context_max_chars = max(800, int(context_max_chars))
        self.last_call_tokens = 0
        self.last_raw_batch = FindingVerificationBatch()
        self.last_post_validation_batch = FindingVerificationBatch()
        self.last_candidate_context: list[dict[str, Any]] = []
        self.last_validation_stats = DeterministicValidationStats()

    async def verify(
        self,
        candidates: list[FindingCandidate],
        request: ReviewRequest,
        state: ContextState,
        *,
        tool_evidence: list[dict[str, Any]] | None = None,
    ) -> FindingVerificationBatch:
        self.last_call_tokens = 0
        self.last_raw_batch = FindingVerificationBatch()
        self.last_post_validation_batch = FindingVerificationBatch()
        self.last_candidate_context = []
        self.last_validation_stats = DeterministicValidationStats()
        if not candidates:
            return FindingVerificationBatch()
        config = self._model_client.default_config.model_copy(deep=True)
        config.temperature = 0.0
        config.tool_choice = {
            "type": "function",
            "function": {"name": "submit_finding_verification"},
        }
        tool = {
            "type": "function",
            "function": {
                "name": "submit_finding_verification",
                "description": "Submit independent verdicts for every candidate finding.",
                "parameters": FindingVerificationBatch.model_json_schema(),
            },
        }
        self.last_candidate_context = build_candidate_verifier_context(
            candidates,
            request,
            tool_evidence or [],
            max_chars=self._context_max_chars,
            context_manifests=[
                dict(item) for item in state.candidate_context_manifests
            ],
            context_mode=state.context_mode,
        )
        payload = {
            "diff": request.diff_text or "",
            "goal": state.goal,
            "constraints": state.constraints,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "candidate_context": self.last_candidate_context,
        }
        response = await self._model_client.chat(
            messages=[
                Message(
                    role="system",
                    content=_COMMON_VERIFIER_SYSTEM_PROMPT
                    + (
                        _AGENT_VERIFIER_POLICY
                        if state.context_mode == "agent_search"
                        else _GRAPH_VERIFIER_POLICY
                    ),
                ),
                Message(role="user", content=json.dumps(payload, ensure_ascii=True)),
            ],
            config=config,
            tools=[tool],
        )
        self.last_call_tokens = response.usage.total_tokens
        batch = _parse_verification_response(response.tool_calls, response.content)
        self.last_raw_batch = _complete_fail_closed(candidates, batch)
        (
            self.last_post_validation_batch,
            self.last_validation_stats,
        ) = validate_verifications_with_stats(
            candidates,
            self.last_raw_batch,
            request,
            candidate_context=self.last_candidate_context,
        )
        return self.last_post_validation_batch


def validate_verifications(
    candidates: list[FindingCandidate],
    batch: FindingVerificationBatch,
    request: ReviewRequest,
    *,
    candidate_context: list[dict[str, Any]] | None = None,
) -> FindingVerificationBatch:
    """Deterministically reject verdicts whose locations lack trusted evidence scope."""
    result, _ = validate_verifications_with_stats(
        candidates,
        batch,
        request,
        candidate_context=candidate_context,
    )
    return result


def validate_verifications_with_stats(
    candidates: list[FindingCandidate],
    batch: FindingVerificationBatch,
    request: ReviewRequest,
    *,
    candidate_context: list[dict[str, Any]] | None = None,
) -> tuple[FindingVerificationBatch, DeterministicValidationStats]:
    """Validate verdicts and report accepted-evidence check statistics."""
    from src.analyzer.schemas import FindingVerification

    changed = changed_new_lines_by_file(request.diff_text or "")
    by_id = {item.candidate_id: item for item in candidates}
    contexts_by_id = {
        str(item.get("candidate_id", "")): item
        for item in candidate_context or []
        if isinstance(item, dict)
    }
    results: list[FindingVerification] = []
    checked_count = 0
    passed_count = 0
    rejected_count = 0
    for verdict in batch.results:
        candidate = by_id.get(verdict.candidate_id)
        valid = candidate is not None
        if verdict.status == "accepted":
            checked_count += 1
            effective_issue = (
                verdict.revised_issue
                if verdict.revised_issue is not None
                else candidate.issue
                if candidate is not None
                else None
            )
            if effective_issue is not None:
                effective_issue = effective_issue.model_copy(
                    update={"candidate_id": verdict.candidate_id}
                )
            requires_revision = bool(
                candidate is not None
                and candidate.candidate_kind
                in {"filter_rescue", "severity_calibration"}
            )
            candidate_location = normalize_location(
                effective_issue.location if effective_issue is not None else ""
            )
            locations = [
                normalize_location(value) for value in verdict.verified_evidence
            ]
            context = contexts_by_id.get(verdict.candidate_id)
            valid = (
                valid
                and effective_issue is not None
                and effective_issue.severity in RISK_SEVERITIES
                and evaluate_issue_filter(effective_issue).passed
                and (not requires_revision or verdict.revised_issue is not None)
                and _location_intersects_changed_lines(candidate_location, changed)
                and bool(locations)
                and all(item.valid and item.line is not None for item in locations)
                and all(
                    _location_intersects_changed_lines(item, changed)
                    or location_in_candidate_context(context, item)
                    for item in locations
                )
            )
            if (
                valid
                and candidate is not None
                and effective_issue is not None
                and effective_issue.is_structured_hypothesis
            ):
                effective_candidate = candidate.model_copy(
                    update={"issue": effective_issue}
                )
                valid = _structured_candidate_evidence_valid(
                    effective_candidate,
                    context,
                    changed,
                )
            if valid:
                passed_count += 1
            else:
                rejected_count += 1
        elif verdict.status == "downgraded":
            valid = (
                valid
                and verdict.revised_issue is not None
                and verdict.revised_issue.severity in {Severity.INFO, Severity.STYLE}
            )
        if not valid:
            results.append(
                FindingVerification(
                    candidate_id=verdict.candidate_id,
                    status="rejected",
                    reason_codes=["deterministic_evidence_invalid"],
                    rationale="Deterministic evidence or downgrade validation failed.",
                )
            )
        else:
            results.append(verdict)
    return (
        FindingVerificationBatch(results=results),
        DeterministicValidationStats(
            checked_count=checked_count,
            passed_count=passed_count,
            rejected_count=rejected_count,
        ),
    )


def review_candidate_severities(
    report: ReviewReport,
    request: ReviewRequest,
) -> SeverityReviewResult:
    """Promote only concrete high-confidence info risks on changed lines."""
    changed = changed_new_lines_by_file(request.diff_text or "")
    high_confidence_info_count = 0
    reviewed_count = 0
    promoted_count = 0
    issues: list[ReviewIssue] = []
    for issue in report.issues:
        if (
            issue.severity != Severity.INFO
            or issue.confidence < _HIGH_CONFIDENCE_INFO_THRESHOLD
        ):
            issues.append(issue)
            continue
        high_confidence_info_count += 1
        location = normalize_location(issue.location)
        if not _location_intersects_changed_lines(location, changed):
            issues.append(issue)
            continue
        reviewed_count += 1
        claim = f"{issue.evidence}\n{issue.suggestion}"
        if not _CONCRETE_RISK_PATTERN.search(claim):
            issues.append(issue)
            continue
        promoted_count += 1
        issues.append(issue.model_copy(update={"severity": Severity.WARNING}))
    return SeverityReviewResult(
        report=ReviewReport(summary=report.summary, issues=issues),
        high_confidence_info_count=high_confidence_info_count,
        reviewed_count=reviewed_count,
        promoted_count=promoted_count,
    )


def _location_intersects_changed_lines(
    location: Any,
    changed: dict[str, set[int]],
) -> bool:
    """Require each evidence range to include at least one changed new line."""
    if not location.valid or location.path not in changed or location.line is None:
        return False
    end_line = location.end_line or location.line
    return any(
        line in changed[location.path] for line in range(location.line, end_line + 1)
    )


def _structured_candidate_evidence_valid(
    candidate: FindingCandidate,
    context: dict[str, Any] | None,
    changed: dict[str, set[int]],
) -> bool:
    """Fail closed when a schema-v2 hypothesis is not bound to sent context."""

    issue = candidate.issue
    anchor = issue.primary_anchor
    if anchor is None or anchor.line not in changed.get(anchor.file, set()):
        return False
    parsed_location = normalize_location(issue.location)
    if (
        not parsed_location.valid
        or parsed_location.path != anchor.file
        or parsed_location.line is None
        or not (
            parsed_location.line
            <= anchor.line
            <= (parsed_location.end_line or parsed_location.line)
        )
    ):
        return False
    if (
        not all(
            value.strip()
            for value in (
                issue.observed_behavior,
                issue.causal_mechanism,
                issue.violated_invariant,
                issue.repair_intent.action,
                issue.repair_intent.boundary,
            )
        )
        or not issue.repair_intent.targets
    ):
        return False
    if not issue.cause_evidence or not issue.contract_evidence:
        return False
    if issue.trigger.strip() and not issue.trigger_evidence:
        return False
    if issue.impact.strip() and not issue.impact_evidence:
        return False
    evidence = issue.all_evidence()
    if not evidence:
        return False
    if any(
        not all(
            value.strip()
            for value in (
                item.candidate_id,
                item.retrieval_source,
                item.file,
                item.statement,
            )
        )
        for item in evidence
    ):
        return False
    if issue.context_manifest_id and any(
        item.context_manifest_id != issue.context_manifest_id for item in evidence
    ):
        return False
    if not issue.context_manifest_id and any(
        item.context_manifest_id for item in evidence
    ):
        return False
    return all(provenance_in_candidate_context(context, item) for item in evidence)


def build_candidates(
    report: ReviewReport,
    *,
    iteration: int,
    request: ReviewRequest | None = None,
    include_boundary: bool = False,
) -> list[FindingCandidate]:
    """Build stable risk and tightly bounded calibration candidates."""
    candidates: list[FindingCandidate] = []
    seen: set[str] = set()
    changed = changed_new_lines_by_file(request.diff_text or "") if request else {}
    for source_issue_index, issue in enumerate(report.issues):
        candidate_kind = ""
        decision = evaluate_issue_filter(issue)
        if issue.severity in RISK_SEVERITIES:
            if not include_boundary or decision.passed:
                candidate_kind = "risk"
            elif (
                request is not None
                and _boundary_issue_eligible(issue, changed)
                and _confidence_only_filter_rejection(decision.reason_codes)
            ):
                candidate_kind = "filter_rescue"
        elif (
            include_boundary
            and request is not None
            and issue.severity == Severity.INFO
            and _boundary_issue_eligible(issue, changed)
            and _describes_current_functional_risk(issue)
        ):
            candidate_kind = "severity_calibration"
        if not candidate_kind:
            continue
        candidate_id = _candidate_id(issue)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        issue.candidate_id = candidate_id
        if not issue.finding_id:
            issue.finding_id = "F-" + candidate_id[:12].upper()
        candidate_issue = issue.model_copy(update={"candidate_id": candidate_id})
        candidates.append(
            FindingCandidate(
                candidate_id=candidate_id,
                issue=candidate_issue,
                claim=issue.suggestion.strip(),
                evidence_locations=[issue.location] if issue.location.strip() else [],
                originating_iteration=max(0, iteration),
                candidate_kind=candidate_kind,
                source_issue_index=source_issue_index,
            )
        )
    return candidates


def _boundary_issue_eligible(
    issue: ReviewIssue,
    changed: dict[str, set[int]],
) -> bool:
    if not issue.is_structured_hypothesis or not has_specific_code_evidence(
        issue.evidence
    ):
        return False
    location = normalize_location(issue.location)
    if not _location_intersects_changed_lines(location, changed):
        return False
    anchor = issue.primary_anchor
    if anchor is None or anchor.line not in changed.get(anchor.file, set()):
        return False
    if (
        not all(
            value.strip()
            for value in (
                issue.observed_behavior,
                issue.causal_mechanism,
                issue.violated_invariant,
                issue.repair_intent.action,
                issue.repair_intent.boundary,
            )
        )
        or not issue.repair_intent.targets
        or not issue.cause_evidence
        or not issue.contract_evidence
    ):
        return False
    return True


def _confidence_only_filter_rejection(reason_codes: tuple[str, ...]) -> bool:
    reasons = set(reason_codes)
    confidence_reasons = {
        "critical_confidence_below_threshold",
        "warning_confidence_below_standard_threshold",
        "warning_confidence_below_relaxed_threshold",
    }
    blocking_non_confidence_reasons = {
        "critical_evidence_not_specific",
        "warning_evidence_not_specific",
        "warning_risk_pattern_missing",
    }
    return bool(reasons & confidence_reasons) and not bool(
        reasons & blocking_non_confidence_reasons
    )


def _describes_current_functional_risk(issue: ReviewIssue) -> bool:
    claim = "\n".join(
        (
            issue.observed_behavior,
            issue.causal_mechanism,
            issue.violated_invariant,
            issue.evidence,
            issue.suggestion,
            issue.impact,
        )
    )
    return bool(
        _CURRENT_FUNCTIONAL_RISK_PATTERN.search(claim)
        and not _NON_RISK_BOUNDARY_PATTERN.search(claim)
    )


def apply_verifications(
    report: ReviewReport,
    batch: FindingVerificationBatch,
    *,
    mode: str,
    candidates: list[FindingCandidate] | None = None,
) -> ReviewReport:
    """Apply verifier verdicts; enforce mode is fail closed for risk findings."""
    if mode != "enforce":
        copied = report.model_copy(deep=True)
        for issue in copied.issues:
            if issue.severity in RISK_SEVERITIES:
                issue.candidate_id = issue.candidate_id or _candidate_id(issue)
                issue.finding_id = issue.finding_id or (
                    "F-" + issue.candidate_id[:12].upper()
                )
        return copied
    verdicts = {item.candidate_id: item for item in batch.results}
    candidates_by_id = {
        item.candidate_id: item for item in candidates or []
    }
    output: list[ReviewIssue] = []
    seen_candidate_ids: set[str] = set()
    for issue in report.issues:
        candidate_id = issue.candidate_id or _candidate_id(issue)
        candidate = candidates_by_id.get(candidate_id)
        if candidate is not None:
            seen_candidate_ids.add(candidate_id)
        if (
            issue.severity not in RISK_SEVERITIES
            and (
                candidate is None
                or candidate.candidate_kind != "severity_calibration"
            )
        ):
            output.append(issue)
            continue
        verdict = verdicts.get(candidate_id)
        if candidate is not None and candidate.candidate_kind == "severity_calibration":
            if (
                verdict is not None
                and verdict.status == "accepted"
                and verdict.revised_issue is not None
                and verdict.revised_issue.severity in RISK_SEVERITIES
            ):
                output.append(
                    _bind_verified_issue(verdict.revised_issue, candidate_id)
                )
            else:
                output.append(issue)
            continue
        if verdict is None:
            continue
        if verdict.status == "accepted":
            accepted_issue = verdict.revised_issue or issue
            output.append(_bind_verified_issue(accepted_issue, candidate_id))
        elif verdict.status == "downgraded" and verdict.revised_issue is not None:
            output.append(_bind_verified_issue(verdict.revised_issue, candidate_id))
    for candidate in candidates or []:
        if (
            candidate.candidate_kind != "filter_rescue"
            or candidate.candidate_id in seen_candidate_ids
        ):
            continue
        verdict = verdicts.get(candidate.candidate_id)
        if (
            verdict is not None
            and verdict.status == "accepted"
            and verdict.revised_issue is not None
            and verdict.revised_issue.severity in RISK_SEVERITIES
        ):
            output.append(
                _bind_verified_issue(verdict.revised_issue, candidate.candidate_id)
            )
    return ReviewReport(summary=report.summary, issues=output)


def _bind_verified_issue(issue: ReviewIssue, candidate_id: str) -> ReviewIssue:
    return issue.model_copy(
        update={
            "candidate_id": candidate_id,
            "finding_id": issue.finding_id or ("F-" + candidate_id[:12].upper()),
        }
    )


def _candidate_id(issue: ReviewIssue) -> str:
    normalized = "\n".join(
        (
            issue.severity.value,
            issue.location.strip().replace("\\", "/"),
            issue.evidence.strip(),
            issue.suggestion.strip(),
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _parse_verification_response(
    tool_calls: list[dict[str, Any]],
    content: str,
) -> FindingVerificationBatch:
    for raw_call in tool_calls:
        function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
        if not isinstance(function, dict):
            continue
        if function.get("name") != "submit_finding_verification":
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return FindingVerificationBatch.model_validate(arguments)
    if content.strip():
        return FindingVerificationBatch.model_validate_json(content)
    raise ValueError("Verifier returned no structured verdict")


def _complete_fail_closed(
    candidates: list[FindingCandidate],
    batch: FindingVerificationBatch,
) -> FindingVerificationBatch:
    expected = {item.candidate_id for item in candidates}
    by_id = {
        item.candidate_id: item
        for item in batch.results
        if item.candidate_id in expected
    }
    from src.analyzer.schemas import FindingVerification

    for candidate in candidates:
        if candidate.candidate_id not in by_id:
            by_id[candidate.candidate_id] = FindingVerification(
                candidate_id=candidate.candidate_id,
                status="rejected",
                reason_codes=["claim_not_supported"],
                rationale="Verifier did not return a verdict for this candidate.",
            )
    return FindingVerificationBatch(
        results=[by_id[item.candidate_id] for item in candidates]
    )
