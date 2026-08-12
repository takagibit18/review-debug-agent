"""Candidate finding normalization and semantic-verification enforcement."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.analyzer.context_state import ContextState
from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.evidence_binding import (
    bind_candidate_evidence,
    bind_issue_candidate_id,
)
from src.analyzer.evidence_policy import evidence_policy_for_mode
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
    FindingCandidateKind,
    FindingVerificationBatch,
    ReviewRequest,
)
from src.analyzer.verifier_context import (
    _location_overlaps_record,
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
    rejection_details: tuple[DeterministicRejectionDetail, ...] = ()


DeterministicRejectionRule = Literal[
    "candidate_binding_missing",
    "finding_primary_location_invalid",
    "finding_not_actionable_risk",
    "verifier_revision_required",
    "verified_evidence_missing",
    "evidence_location_missing",
    "evidence_context_missing",
    "diff_evidence_context_missing",
    "tool_evidence_context_missing",
    "manifest_id_mismatch",
    "manifest_hash_mismatch",
    "evidence_binding_missing",
    "structured_finding_incomplete",
    "required_evidence_missing",
    "graph_evidence_invalid",
    "revised_evidence_invalid",
    "downgrade_invalid",
    "deterministic_validation_failed",
]


class DeterministicRejectionDetail(BaseModel):
    """One exact fail-closed rule tied to a finding or evidence item."""

    candidate_id: str
    finding_id: str = ""
    rule: DeterministicRejectionRule
    evidence_role: str = "finding"
    evidence_index: int | None = Field(default=None, ge=0)
    retrieval_source: str = ""
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    field: str = ""
    revised_issue: bool = False
    message: str


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
        manifests = [dict(item) for item in state.candidate_context_manifests]
        # Candidate identity and provenance labels are system-owned. Bind against
        # all successful run evidence before building the bounded verifier envelope
        # so a corrected source receives first-pass retention priority.
        candidates[:] = bind_candidate_evidence(
            candidates,
            request,
            tool_evidence or [],
            context_manifests=manifests,
        )
        self.last_candidate_context = build_candidate_verifier_context(
            candidates,
            request,
            tool_evidence or [],
            max_chars=self._context_max_chars,
            context_manifests=manifests,
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
    rejection_details: list[DeterministicRejectionDetail] = []
    for verdict in batch.results:
        candidate = by_id.get(verdict.candidate_id)
        valid = candidate is not None
        verdict_rejections: list[DeterministicRejectionDetail] = []
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
            verdict_rejections = _accepted_verdict_rejections(
                verdict_candidate_id=verdict.candidate_id,
                candidate=candidate,
                effective_issue=effective_issue,
                context=context,
                changed=changed,
                requires_revision=requires_revision,
                revised_issue=verdict.revised_issue is not None,
                verified_evidence=verdict.verified_evidence,
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
                verdict_rejections = [
                    _rejection_detail(
                        verdict.candidate_id,
                        candidate.issue if candidate is not None else None,
                        "downgrade_invalid",
                        field="revised_issue.severity",
                        revised_issue=verdict.revised_issue is not None,
                        message=(
                            "A downgraded verdict must bind to an existing candidate "
                            "and provide an info/style revised issue."
                        ),
                    )
                ]
        elif not valid:
            verdict_rejections = [
                _rejection_detail(
                    verdict.candidate_id,
                    None,
                    "candidate_binding_missing",
                    field="candidate_id",
                    message="The verifier verdict candidate_id does not match a finding.",
                )
            ]
        if not valid:
            if not verdict_rejections:
                verdict_rejections = [
                    _rejection_detail(
                        verdict.candidate_id,
                        candidate.issue if candidate is not None else None,
                        "deterministic_validation_failed",
                        message=(
                            "Deterministic validation failed without a more specific "
                            "diagnostic; fail closed."
                        ),
                    )
                ]
            rejection_details.extend(verdict_rejections)
            rule_summary = ", ".join(
                dict.fromkeys(item.rule for item in verdict_rejections)
            )
            results.append(
                FindingVerification(
                    candidate_id=verdict.candidate_id,
                    status="rejected",
                    reason_codes=["deterministic_evidence_invalid"],
                    rationale=f"Deterministic validation failed: {rule_summary}.",
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
            rejection_details=tuple(rejection_details),
        ),
    )


def _accepted_verdict_rejections(
    *,
    verdict_candidate_id: str,
    candidate: FindingCandidate | None,
    effective_issue: ReviewIssue | None,
    context: dict[str, Any] | None,
    changed: dict[str, set[int]],
    requires_revision: bool,
    revised_issue: bool,
    verified_evidence: list[str],
) -> list[DeterministicRejectionDetail]:
    """Explain every failed rule without changing the fail-closed decision."""

    if candidate is None or effective_issue is None:
        return [
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "candidate_binding_missing",
                field="candidate_id",
                revised_issue=revised_issue,
                message="The accepted verdict candidate_id does not match a finding.",
            )
        ]

    details: list[DeterministicRejectionDetail] = []
    parsed_location = normalize_location(effective_issue.location)
    if not _location_intersects_changed_lines(parsed_location, changed):
        details.append(
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "finding_primary_location_invalid",
                file=parsed_location.path or "",
                line=parsed_location.line,
                end_line=parsed_location.end_line,
                field="location",
                revised_issue=revised_issue,
                message="The finding primary location does not intersect a changed line.",
            )
        )
    if (
        effective_issue.severity not in RISK_SEVERITIES
        or not evaluate_issue_filter(effective_issue).passed
    ):
        details.append(
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "finding_not_actionable_risk",
                field="severity/evidence/confidence",
                revised_issue=revised_issue,
                message="The accepted issue does not pass the existing risk policy.",
            )
        )
    if requires_revision and not revised_issue:
        details.append(
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "verifier_revision_required",
                field="revised_issue",
                message="This candidate kind requires a verifier-authored revised issue.",
            )
        )

    if not verified_evidence:
        details.append(
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "verified_evidence_missing",
                evidence_role="verifier",
                field="verified_evidence",
                revised_issue=revised_issue,
                message="The accepted verdict did not identify any verified code location.",
            )
        )
    for index, raw_location in enumerate(verified_evidence):
        location = normalize_location(raw_location)
        if not location.valid or location.line is None:
            details.append(
                _rejection_detail(
                    verdict_candidate_id,
                    effective_issue,
                    "evidence_location_missing",
                    evidence_role="verifier",
                    evidence_index=index,
                    field="verified_evidence",
                    revised_issue=revised_issue,
                    message=(
                        "The verifier evidence entry does not contain a valid code "
                        f"location: {raw_location!r}."
                    ),
                )
            )
            continue
        if not (
            _location_intersects_changed_lines(location, changed)
            or location_in_candidate_context(context, location)
        ):
            details.append(
                _rejection_detail(
                    verdict_candidate_id,
                    effective_issue,
                    "evidence_context_missing",
                    evidence_role="verifier",
                    evidence_index=index,
                    file=location.path or "",
                    line=location.line,
                    end_line=location.end_line,
                    field="verified_evidence",
                    revised_issue=revised_issue,
                    message="The cited verifier evidence was not retained in candidate context.",
                )
            )

    if effective_issue.is_structured_hypothesis:
        details.extend(
            _structured_candidate_evidence_rejections(
                candidate.model_copy(update={"issue": effective_issue}),
                context,
                changed,
                revised_issue=revised_issue,
            )
        )
    if revised_issue and details:
        details.append(
            _rejection_detail(
                verdict_candidate_id,
                effective_issue,
                "revised_evidence_invalid",
                field="revised_issue",
                revised_issue=True,
                message="The verifier-revised finding did not pass deterministic revalidation.",
            )
        )
    return details


def _structured_candidate_evidence_rejections(
    candidate: FindingCandidate,
    context: dict[str, Any] | None,
    changed: dict[str, set[int]],
    *,
    revised_issue: bool,
) -> list[DeterministicRejectionDetail]:
    """Return field- and evidence-level failures for a schema-v2 finding."""

    issue = candidate.issue
    details: list[DeterministicRejectionDetail] = []
    anchor = issue.primary_anchor
    if anchor is None or anchor.line not in changed.get(anchor.file, set()):
        details.append(
            _rejection_detail(
                candidate.candidate_id,
                issue,
                "finding_primary_location_invalid",
                file=anchor.file if anchor is not None else "",
                line=anchor.line if anchor is not None else None,
                end_line=anchor.end_line if anchor is not None else None,
                field="primary_anchor",
                revised_issue=revised_issue,
                message="The structured primary anchor is absent or not a changed line.",
            )
        )
    parsed_location = normalize_location(issue.location)
    if (
        anchor is None
        or not parsed_location.valid
        or parsed_location.path != anchor.file
        or parsed_location.line is None
        or not (
            parsed_location.line
            <= anchor.line
            <= (parsed_location.end_line or parsed_location.line)
        )
    ):
        details.append(
            _rejection_detail(
                candidate.candidate_id,
                issue,
                "finding_primary_location_invalid",
                file=parsed_location.path or "",
                line=parsed_location.line,
                end_line=parsed_location.end_line,
                field="location/primary_anchor",
                revised_issue=revised_issue,
                message="The legacy location and structured primary anchor do not agree.",
            )
        )

    required_fields = {
        "observed_behavior": issue.observed_behavior,
        "causal_mechanism": issue.causal_mechanism,
        "violated_invariant": issue.violated_invariant,
        "repair_intent.action": issue.repair_intent.action,
        "repair_intent.boundary": issue.repair_intent.boundary,
    }
    missing_fields = [
        name for name, value in required_fields.items() if not value.strip()
    ]
    if not issue.repair_intent.targets:
        missing_fields.append("repair_intent.targets")
    if missing_fields:
        details.append(
            _rejection_detail(
                candidate.candidate_id,
                issue,
                "structured_finding_incomplete",
                field=",".join(missing_fields),
                revised_issue=revised_issue,
                message="Required structured finding fields are empty.",
            )
        )

    required_roles: list[str] = []
    if not issue.cause_evidence:
        required_roles.append("cause")
    if not issue.contract_evidence:
        required_roles.append("contract")
    if issue.trigger.strip() and not issue.trigger_evidence:
        required_roles.append("trigger")
    if issue.impact.strip() and not issue.impact_evidence:
        required_roles.append("impact")
    for role in required_roles:
        details.append(
            _rejection_detail(
                candidate.candidate_id,
                issue,
                "required_evidence_missing",
                evidence_role=role,
                field=f"{role}_evidence",
                revised_issue=revised_issue,
                message=f"The structured {role} claim has no evidence entry.",
            )
        )

    evidence_by_role = (
        ("cause", issue.cause_evidence),
        ("contract", issue.contract_evidence),
        ("trigger", issue.trigger_evidence),
        ("impact", issue.impact_evidence),
    )
    if not issue.all_evidence():
        details.append(
            _rejection_detail(
                candidate.candidate_id,
                issue,
                "evidence_binding_missing",
                field="all_evidence",
                revised_issue=revised_issue,
                message="The finding has no structured evidence bound to it.",
            )
        )
    for role, evidence_items in evidence_by_role:
        for index, evidence in enumerate(evidence_items):
            details.extend(
                _evidence_rejections(
                    candidate,
                    issue,
                    context,
                    evidence,
                    evidence_role=role,
                    evidence_index=index,
                    revised_issue=revised_issue,
                )
            )
    return details


def _evidence_rejections(
    candidate: FindingCandidate,
    issue: ReviewIssue,
    context: dict[str, Any] | None,
    evidence: Any,
    *,
    evidence_role: str,
    evidence_index: int,
    revised_issue: bool,
) -> list[DeterministicRejectionDetail]:
    """Diagnose one structured evidence item against the retained context."""

    candidate_id = str(getattr(evidence, "candidate_id", ""))
    retrieval_source = str(getattr(evidence, "retrieval_source", "")).strip()
    file = str(getattr(evidence, "file", ""))
    line = getattr(evidence, "line", None)
    end_line = getattr(evidence, "end_line", None)
    statement = str(getattr(evidence, "statement", ""))
    evidence_manifest = str(getattr(evidence, "context_manifest_id", ""))
    digest = str(getattr(evidence, "context_hash", ""))

    def detail(
        rule: DeterministicRejectionRule,
        *,
        field: str,
        message: str,
    ) -> DeterministicRejectionDetail:
        return _rejection_detail(
            candidate.candidate_id,
            issue,
            rule,
            evidence_role=evidence_role,
            evidence_index=evidence_index,
            retrieval_source=retrieval_source,
            file=file,
            line=line,
            end_line=end_line,
            field=field,
            revised_issue=revised_issue,
            message=message,
        )

    details: list[DeterministicRejectionDetail] = []
    if not candidate_id:
        details.append(
            detail(
                "evidence_binding_missing",
                field="candidate_id",
                message="The evidence item is not bound to a finding candidate.",
            )
        )
    if not file or line is None:
        details.append(
            detail(
                "evidence_location_missing",
                field="file/line",
                message="The evidence item does not identify a code location.",
            )
        )
    if not retrieval_source:
        details.append(
            detail(
                "evidence_context_missing",
                field="retrieval_source",
                message="The evidence item does not declare a retrieval source.",
            )
        )
    if not statement.strip():
        details.append(
            detail(
                "evidence_binding_missing",
                field="statement",
                message="The evidence item has no statement tying code to the finding.",
            )
        )
    if issue.context_manifest_id:
        if evidence_manifest != issue.context_manifest_id:
            details.append(
                detail(
                    "manifest_id_mismatch",
                    field="context_manifest_id",
                    message=(
                        "The evidence manifest id does not match the finding manifest id."
                    ),
                )
            )
    elif evidence_manifest:
        details.append(
            detail(
                "manifest_id_mismatch",
                field="context_manifest_id",
                message="Evidence declares a manifest that the finding does not bind.",
            )
        )

    if (
        candidate_id
        and file
        and line is not None
        and retrieval_source
        and not (
            issue.context_manifest_id and evidence_manifest != issue.context_manifest_id
        )
        and not (not issue.context_manifest_id and evidence_manifest)
    ):
        provenance_rule = _provenance_rejection_rule(context, evidence)
        if provenance_rule is not None:
            rule, field, message = provenance_rule
            details.append(
                detail(
                    rule,
                    field=field,
                    message=message,
                )
            )
    if evidence_manifest and not digest:
        # This is already rejected by provenance validation, but naming the exact
        # missing field is more useful than a generic context miss.
        if not any(item.rule == "manifest_hash_mismatch" for item in details):
            details.append(
                detail(
                    "manifest_hash_mismatch",
                    field="context_hash",
                    message="Manifest evidence is missing its required context hash.",
                )
            )
    return details


def _provenance_rejection_rule(
    context: dict[str, Any] | None,
    evidence: Any,
) -> tuple[DeterministicRejectionRule, str, str] | None:
    """Return the precise reason for the existing provenance predicate failure."""

    if provenance_in_candidate_context(context, evidence):
        return None
    if context is None:
        return (
            "evidence_context_missing",
            "candidate_context",
            "No retained candidate context exists for this evidence item.",
        )
    mode = str(context.get("context_mode", "graph_hybrid"))
    policy = evidence_policy_for_mode(
        "agent_search" if mode == "agent_search" else "graph_hybrid"
    )
    policy_raw = context.get("evidence_policy", {})
    if isinstance(policy_raw, dict) and policy_raw:
        try:
            policy = policy.model_validate(policy_raw)
        except Exception:  # noqa: BLE001
            pass
    manifest_id = str(context.get("context_manifest_id", ""))
    evidence_manifest = str(getattr(evidence, "context_manifest_id", ""))
    file = str(getattr(evidence, "file", "")).replace("\\", "/")
    line = int(getattr(evidence, "line", 0) or 0)
    end_line = int(getattr(evidence, "end_line", 0) or line)
    digest = str(getattr(evidence, "context_hash", ""))
    retrieval_source = str(getattr(evidence, "retrieval_source", "")).strip().lower()
    if evidence_manifest:
        if (
            not policy.allow_manifest_evidence
            or not manifest_id
            or evidence_manifest != manifest_id
        ):
            return (
                "manifest_id_mismatch",
                "context_manifest_id",
                "Manifest evidence does not match an allowed retained manifest.",
            )
        spans = context.get("included_spans")
        overlapping_spans = [
            span
            for span in spans or []
            if isinstance(span, dict)
            and _location_overlaps_record(file, line, end_line, span)
        ]
        if not digest or (
            overlapping_spans
            and all(
                str(span.get("context_hash", "")) != digest
                for span in overlapping_spans
            )
        ):
            return (
                "manifest_hash_mismatch",
                "context_hash",
                "Manifest evidence hash does not match the retained code span.",
            )
        if not overlapping_spans:
            return (
                "evidence_context_missing",
                "file/line",
                "The manifest evidence location is not present in retained manifest spans.",
            )
        if getattr(evidence, "edge_kind", ""):
            return (
                "graph_evidence_invalid",
                "edge_kind/edge_confidence/resolver/evidence_eligibility",
                "Graph evidence does not satisfy the retained strong-edge contract.",
            )
        return None
    if policy.require_manifest:
        return (
            "manifest_id_mismatch",
            "context_manifest_id",
            "The active evidence policy requires a manifest binding.",
        )
    if getattr(evidence, "edge_kind", ""):
        return (
            "graph_evidence_invalid",
            "edge_kind",
            "Graph edge evidence cannot be used without manifest provenance.",
        )
    if retrieval_source in {"git_diff", "diff", "review_diff", "changed_hunk"}:
        return (
            "diff_evidence_context_missing",
            "retrieval_source/file/line",
            "Evidence claims diff provenance but its location is not in retained diff context.",
        )
    return (
        "tool_evidence_context_missing",
        "retrieval_source/file/line",
        "Evidence claims file/tool provenance without a matching successful retained read.",
    )


def _rejection_detail(
    candidate_id: str,
    issue: ReviewIssue | None,
    rule: DeterministicRejectionRule,
    *,
    evidence_role: str = "finding",
    evidence_index: int | None = None,
    retrieval_source: str = "",
    file: str = "",
    line: int | None = None,
    end_line: int | None = None,
    field: str = "",
    revised_issue: bool = False,
    message: str,
) -> DeterministicRejectionDetail:
    """Construct a stable, event-log-safe deterministic rejection record."""

    return DeterministicRejectionDetail(
        candidate_id=candidate_id,
        finding_id=issue.finding_id if issue is not None else "",
        rule=rule,
        evidence_role=evidence_role,
        evidence_index=evidence_index,
        retrieval_source=retrieval_source,
        file=file,
        line=line,
        end_line=end_line,
        field=field,
        revised_issue=revised_issue,
        message=message,
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
        candidate_kind: FindingCandidateKind | None = None
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
        if candidate_kind is None:
            continue
        candidate_id = _candidate_id(issue)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        bound_issue = bind_issue_candidate_id(issue, candidate_id)
        issue.candidate_id = candidate_id
        for evidence in issue.all_evidence():
            evidence.candidate_id = candidate_id
        if not issue.finding_id:
            issue.finding_id = "F-" + candidate_id[:12].upper()
        bound_issue.finding_id = issue.finding_id
        candidate_issue = bound_issue
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
    candidates_by_id = {item.candidate_id: item for item in candidates or []}
    output: list[ReviewIssue] = []
    seen_candidate_ids: set[str] = set()
    for issue in report.issues:
        candidate_id = issue.candidate_id or _candidate_id(issue)
        candidate = candidates_by_id.get(candidate_id)
        if candidate is not None:
            seen_candidate_ids.add(candidate_id)
        if issue.severity not in RISK_SEVERITIES and (
            candidate is None or candidate.candidate_kind != "severity_calibration"
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
                output.append(_bind_verified_issue(verdict.revised_issue, candidate_id))
            else:
                output.append(issue)
            continue
        if verdict is None:
            continue
        if verdict.status == "accepted":
            accepted_issue = verdict.revised_issue or (
                candidate.issue if candidate is not None else issue
            )
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
    bound = bind_issue_candidate_id(issue, candidate_id)
    return bound.model_copy(
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
