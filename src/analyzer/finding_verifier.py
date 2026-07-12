"""Candidate finding normalization and semantic-verification enforcement."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import (
    FindingCandidate,
    FindingVerificationBatch,
    ReviewRequest,
)
from src.models.schemas import Message

RISK_SEVERITIES = {Severity.CRITICAL, Severity.WARNING}

_VERIFIER_SYSTEM_PROMPT = """You are an independent semantic verifier for PR review findings.
Treat every candidate as untrusted. Verify that the cited code exists in the supplied diff,
that the causal claim follows from the evidence, that the PR introduced the behavior, and
that the suggestion is actionable. Seek counterexamples. Return exactly one structured
verdict per candidate through submit_finding_verification. Never accept a candidate merely
because its confidence is high."""


class FindingVerifier:
    """Run one bounded, submit-only semantic verification model call."""

    def __init__(self, model_client: Any) -> None:
        self._model_client = model_client
        self.last_call_tokens = 0

    async def verify(
        self,
        candidates: list[FindingCandidate],
        request: ReviewRequest,
        state: ContextState,
    ) -> FindingVerificationBatch:
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
        payload = {
            "diff": request.diff_text or "",
            "goal": state.goal,
            "constraints": state.constraints,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
        response = await self._model_client.chat(
            messages=[
                Message(role="system", content=_VERIFIER_SYSTEM_PROMPT),
                Message(role="user", content=json.dumps(payload, ensure_ascii=True)),
            ],
            config=config,
            tools=[tool],
        )
        self.last_call_tokens = response.usage.total_tokens
        batch = _parse_verification_response(response.tool_calls, response.content)
        return validate_verifications(candidates, _complete_fail_closed(candidates, batch), request)


def validate_verifications(
    candidates: list[FindingCandidate],
    batch: FindingVerificationBatch,
    request: ReviewRequest,
) -> FindingVerificationBatch:
    """Deterministically reject verdicts whose locations are not bound to the diff."""
    from src.analyzer.schemas import FindingVerification

    changed = changed_new_lines_by_file(request.diff_text or "")
    by_id = {item.candidate_id: item for item in candidates}
    results: list[FindingVerification] = []
    for verdict in batch.results:
        candidate = by_id.get(verdict.candidate_id)
        valid = candidate is not None
        if verdict.status == "accepted":
            locations = [normalize_location(value) for value in verdict.verified_evidence]
            valid = valid and bool(locations) and all(
                _location_intersects_changed_lines(item, changed)
                for item in locations
            )
        elif verdict.status == "downgraded":
            valid = (
                valid
                and verdict.revised_issue is not None
                and verdict.revised_issue.severity in {Severity.INFO, Severity.STYLE}
            )
        if not valid:
            results.append(FindingVerification(
                candidate_id=verdict.candidate_id,
                status="rejected",
                reason_codes=["evidence_not_found"],
                rationale="Deterministic evidence or downgrade validation failed.",
            ))
        else:
            results.append(verdict)
    return FindingVerificationBatch(results=results)


def _location_intersects_changed_lines(
    location: Any,
    changed: dict[str, set[int]],
) -> bool:
    """Require each evidence range to include at least one changed new line."""
    if not location.valid or location.path not in changed or location.line is None:
        return False
    end_line = location.end_line or location.line
    return any(
        line in changed[location.path]
        for line in range(location.line, end_line + 1)
    )


def build_candidates(
    report: ReviewReport,
    *,
    iteration: int,
) -> list[FindingCandidate]:
    """Build stable candidates for findings that can produce advisory risk comments."""
    candidates: list[FindingCandidate] = []
    for issue in report.issues:
        if issue.severity not in RISK_SEVERITIES:
            continue
        candidate_id = _candidate_id(issue)
        candidate_issue = issue.model_copy(update={"candidate_id": candidate_id})
        candidates.append(
            FindingCandidate(
                candidate_id=candidate_id,
                issue=candidate_issue,
                claim=issue.suggestion.strip(),
                evidence_locations=[issue.location] if issue.location.strip() else [],
                originating_iteration=max(0, iteration),
            )
        )
    return candidates


def apply_verifications(
    report: ReviewReport,
    batch: FindingVerificationBatch,
    *,
    mode: str,
) -> ReviewReport:
    """Apply verifier verdicts; enforce mode is fail closed for risk findings."""
    if mode != "enforce":
        return report.model_copy(deep=True)
    verdicts = {item.candidate_id: item for item in batch.results}
    output: list[ReviewIssue] = []
    for issue in report.issues:
        if issue.severity not in RISK_SEVERITIES:
            output.append(issue)
            continue
        candidate_id = issue.candidate_id or _candidate_id(issue)
        verdict = verdicts.get(candidate_id)
        if verdict is None:
            continue
        if verdict.status == "accepted":
            output.append(issue.model_copy(update={"candidate_id": candidate_id}))
        elif verdict.status == "downgraded" and verdict.revised_issue is not None:
            output.append(
                verdict.revised_issue.model_copy(update={"candidate_id": candidate_id})
            )
    return ReviewReport(summary=report.summary, issues=output)


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
