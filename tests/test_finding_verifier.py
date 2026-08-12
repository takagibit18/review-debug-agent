"""Tests for semantic finding verification and review-loop enforcement."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RelatedLocation,
    RepairIntent,
    SourceAnchor,
)
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.context_state import ContextState
from src.analyzer.schemas import AnalysisPlan, ReviewRequest
from src.models.schemas import ModelConfig, ModelResponse, TokenUsage
from src.orchestrator.agent_loop import AgentOrchestrator


def _issue(severity: Severity = Severity.WARNING) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        location="pkg/service.py:12",
        evidence="`return cache[key]` executes before the key is populated",
        suggestion="Guard the lookup or populate the cache before reading it.",
        confidence=0.92,
    )


def test_build_candidates_uses_stable_ids_and_only_risk_findings() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(
        summary="review",
        issues=[_issue(), _issue(Severity.INFO)],
    )

    first = module.build_candidates(report, iteration=2)
    second = module.build_candidates(report, iteration=2)

    assert len(first) == 1
    assert first[0].candidate_id == second[0].candidate_id
    assert len(first[0].candidate_id) == 16
    assert first[0].issue.candidate_id == first[0].candidate_id
    assert first[0].originating_iteration == 2
    assert first[0].candidate_kind == "risk"
    assert first[0].source_issue_index == 0


def test_build_candidates_routes_only_structured_boundary_risks() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    rescue = _structured_boundary_issue(Severity.WARNING, confidence=0.65)
    calibration = _structured_boundary_issue(
        Severity.INFO,
        confidence=0.75,
        evidence=(
            "`self.obj == other` compares the current wrapped operand directly "
            "against its wrapper."
        ),
        suggestion=(
            "Compare with `other.obj`; current wrapper equality returns an "
            "incorrect result."
        ),
    ).model_copy(
        update={
            "observed_behavior": "Equal wrapped values currently compare unequal.",
            "causal_mechanism": (
                "The implementation compares the wrapped operand to the wrapper object."
            ),
            "violated_invariant": "Wrapper equality must compare wrapped operands.",
        }
    )
    optimization = _structured_boundary_issue(
        Severity.INFO,
        confidence=0.90,
        evidence="`cache[key]` can be stored in a local variable.",
        suggestion="Optional readability optimization for future maintenance.",
    )
    vague_future = _structured_boundary_issue(
        Severity.WARNING,
        confidence=0.65,
        evidence="This might matter if a future caller changes the wrapper.",
        suggestion="Consider revisiting this in the future.",
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )

    candidates = module.build_candidates(
        ReviewReport(
            summary="review",
            issues=[rescue, calibration, optimization, vague_future],
        ),
        iteration=3,
        request=request,
        include_boundary=True,
    )

    assert [item.candidate_kind for item in candidates] == [
        "filter_rescue",
        "severity_calibration",
    ]
    assert [item.source_issue_index for item in candidates] == [0, 1]


def test_verifier_guidance_prefers_narrow_revision_over_wholesale_rejection() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    prompt = module._COMMON_VERIFIER_SYSTEM_PROMPT  # noqa: SLF001

    assert "status=accepted with revised_issue" in prompt
    assert "candidate_kind=filter_rescue" in prompt
    assert "severity_calibration" in prompt
    assert "pre-change fallback or compatibility" in prompt
    assert "wrapper unwrapping" in prompt
    assert "never raise confidence merely" in prompt
    assert "primary_anchor identify where the problem is best displayed" in prompt
    assert "cause_evidence location must intersect a real changed line" in prompt
    assert "Every code location in revised_issue" in prompt
    assert "Do not change schema_version" in prompt


def test_orchestrator_accepts_revised_boundary_calibration_and_rescue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")
    monkeypatch.setenv("ROOT_CAUSE_CONSOLIDATION_ENABLED", "false")
    monkeypatch.chdir(tmp_path)

    class CalibratingVerifier:
        def __init__(self) -> None:
            self.kinds: list[str] = []

        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            self.kinds = [item.candidate_kind for item in candidates]
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="The diff confirms the current fallback regression.",
                        verified_evidence=["pkg/service.py:12"],
                        revised_issue=item.issue.model_copy(
                            update={
                                "severity": Severity.WARNING,
                                "confidence": 0.90,
                                "suggestion": (
                                    "Restore the fallback to prevent this current "
                                    f"user-visible regression ({item.candidate_kind})."
                                ),
                            }
                        ),
                    )
                    for item in candidates
                ]
            )

    verifier = CalibratingVerifier()
    orchestrator = AgentOrchestrator(
        finding_verifier=verifier,
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
        review_diff_first_changed_files=False,
    )
    submitted = ReviewReport(
        summary="review",
        issues=[
            _structured_boundary_issue(Severity.WARNING, confidence=0.65),
            _structured_boundary_issue(Severity.INFO, confidence=0.75).model_copy(
                update={"finding_id": "F-calibration"}
            ),
        ],
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(draft_review=submitted)

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                    "@@ -11,0 +12,1 @@\n+return cache[key]\n"
                ),
            )
        )
    )

    assert verifier.kinds == ["filter_rescue", "severity_calibration"]
    assert len(response.report.issues) == 2
    assert all(item.severity == Severity.WARNING for item in response.report.issues)
    assert all(item.confidence == 0.90 for item in response.report.issues)
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    funnel = next(
        event for event in events if event["event_type"] == "finding_candidates_built"
    )
    assert funnel["payload"]["filter_rescue_candidate_count"] == 1
    assert funnel["payload"]["severity_calibration_candidate_count"] == 1
    completed_funnel = next(
        event for event in events if event["event_type"] == "finding_funnel_completed"
    )
    assert completed_funnel["payload"]["submitted_finding_count"] == 2
    assert completed_funnel["payload"]["calibration_rescue_candidate_count"] == 2
    assert completed_funnel["payload"]["pre_verifier_rejected_count"] == 0
    assert completed_funnel["payload"]["semantic_rejected_count"] == 0
    assert completed_funnel["payload"]["deterministic_rejected_count"] == 0
    assert completed_funnel["payload"]["final_risk_finding_count"] == 2


def test_apply_verifications_is_fail_closed_in_enforce_mode() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    report = ReviewReport(
        summary="review",
        issues=[_issue(), _issue(Severity.INFO)],
    )
    candidates = module.build_candidates(report, iteration=0)
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidates[0].candidate_id,
                status="rejected",
                reason_codes=["claim_not_supported"],
                rationale="The evidence does not establish the claimed ordering.",
            )
        ]
    )

    enforced = module.apply_verifications(report, batch, mode="enforce")
    shadow = module.apply_verifications(report, batch, mode="shadow")

    assert [issue.severity for issue in enforced.issues] == [Severity.INFO]
    assert len(shadow.issues) == 2


def test_apply_verifications_accepts_revised_downgraded_issue() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]
    revised = _issue(Severity.INFO)
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="downgraded",
                reason_codes=["severity_overstated"],
                rationale="The behavior is advisory rather than a regression.",
                revised_issue=revised,
            )
        ]
    )

    result = module.apply_verifications(report, batch, mode="enforce")

    assert len(result.issues) == 1
    assert result.issues[0].severity == Severity.INFO
    assert result.issues[0].candidate_id == candidate.candidate_id


def test_orchestrator_enforce_mode_removes_rejected_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")

    class RejectingVerifier:
        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="rejected",
                        reason_codes=["claim_not_supported"],
                        rationale="No causal chain in the referenced code.",
                    )
                    for item in candidates
                ]
            )

    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator(
        finding_verifier=RejectingVerifier(),
        finding_verifier_mode="enforce",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(summary="review", issues=[_issue()])
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)

    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path), diff_mode=True))
    )

    assert response.report.issues == []
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event_type"] == "finding_candidates_built" for event in events)
    assert any(
        event["event_type"] == "finding_verification_completed" for event in events
    )


def _structured_boundary_issue(
    severity: Severity,
    *,
    confidence: float,
    evidence: str = "`return cache[key]` now bypasses the compatibility fallback.",
    suggestion: str = "Restore the fallback to prevent a user-visible regression.",
) -> ReviewIssue:
    cause = EvidenceProvenance(
        candidate_id="F-boundary",
        retrieval_source="git_diff",
        file="pkg/service.py",
        line=12,
        statement="The changed return bypasses the fallback.",
    )
    contract = cause.model_copy(
        update={"statement": "Existing callers rely on the compatibility fallback."}
    )
    return ReviewIssue(
        severity=severity,
        location="pkg/service.py:12",
        evidence=evidence,
        suggestion=suggestion,
        confidence=confidence,
        schema_version="2.0",
        finding_id="F-boundary",
        primary_anchor=SourceAnchor(file="pkg/service.py", line=12),
        observed_behavior="Current callers receive the raw cache lookup result.",
        causal_mechanism="The changed return bypasses the existing fallback branch.",
        violated_invariant="The compatibility fallback must remain available.",
        repair_intent=RepairIntent(
            action="Restore the fallback branch",
            targets=["service.load"],
            boundary="cache compatibility contract",
        ),
        cause_evidence=[cause],
        contract_evidence=[contract],
    )


def test_orchestrator_records_submitted_findings_before_policy_filter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator(
        finding_verifier_mode="off",
        review_workflow_enforcement="off",
    )
    submitted_issues = [
        ReviewIssue(
            severity=Severity.WARNING,
            location="pkg/service.py:12",
            evidence="Looks suspicious.",
            suggestion="Investigate this later.",
            confidence=0.65,
        ),
        ReviewIssue(
            severity=Severity.INFO,
            location="pkg/service.py:13",
            evidence="`lookup()` could use a local variable.",
            suggestion="Consider a small readability cleanup.",
            confidence=0.40,
        ),
    ]

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(summary="review", issues=submitted_issues)
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path), diff_mode=True))
    )

    assert [issue.severity for issue in response.report.issues] == [Severity.INFO]
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    decisions = [
        event for event in events if event["event_type"] == "finding_filter_decision"
    ]
    assert len(decisions) == 2
    assert decisions[0]["payload"]["passed"] is False
    assert decisions[0]["payload"]["reason_codes"] == [
        "warning_confidence_below_standard_threshold",
        "warning_confidence_below_relaxed_threshold",
        "warning_evidence_not_specific",
        "warning_risk_pattern_missing",
    ]
    funnel = next(
        event for event in events if event["event_type"] == "finding_candidates_built"
    )
    assert funnel["payload"]["model_raw_issue_count"] == 2
    assert funnel["payload"]["submitted_issue_count"] == 2
    assert funnel["payload"]["policy_passed_issue_count"] == 1
    assert funnel["payload"]["policy_rejected_issue_count"] == 1
    assert funnel["payload"]["non_risk_issue_count"] == 1


def test_orchestrator_does_not_revalidate_builtin_verifier_without_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")

    class ContextValidatedVerifier(module.FindingVerifier):
        def __init__(self) -> None:
            self.last_call_tokens = 0
            self.last_raw_batch = schemas.FindingVerificationBatch()
            self.last_post_validation_batch = schemas.FindingVerificationBatch()
            self.last_validation_stats = module.DeterministicValidationStats()

        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            accepted = schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="Retained symbol context supports the finding.",
                        verified_evidence=["pkg/service.py:11"],
                    )
                    for item in candidates
                ]
            )
            self.last_raw_batch = accepted
            self.last_post_validation_batch = accepted
            self.last_validation_stats = module.DeterministicValidationStats(
                checked_count=1, passed_count=1
            )
            return accepted

    orchestrator = AgentOrchestrator(
        finding_verifier=ContextValidatedVerifier(),
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(summary="review", issues=[_issue()])
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                    "@@ -11,2 +11,3 @@\n guard = ready\n+return cache[key]\n cleanup()\n"
                ),
            )
        )
    )

    assert len(response.report.issues) == 1
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    verification = next(
        event
        for event in events
        if event["event_type"] == "finding_verification_completed"
    )
    assert verification["payload"]["raw_accepted_count"] == 1
    assert verification["payload"]["accepted_count"] == 1
    assert verification["payload"]["deterministic_evidence_checked_count"] == 1
    assert verification["payload"]["deterministic_evidence_passed_count"] == 1


def test_orchestrator_strictly_validates_injected_verifier(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")

    class UnvalidatedVerifier:
        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="This location is outside the diff.",
                        verified_evidence=["pkg/service.py:11"],
                    )
                    for item in candidates
                ]
            )

    orchestrator = AgentOrchestrator(
        finding_verifier=UnvalidatedVerifier(),
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(summary="review", issues=[_issue()])
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                    "@@ -11,0 +12,1 @@\n+return cache[key]\n"
                ),
            )
        )
    )

    assert response.report.issues == []
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    verification = next(
        event
        for event in events
        if event["event_type"] == "finding_verification_completed"
    )
    assert verification["payload"]["deterministic_rejection_details"] == [
        {
            "candidate_id": verification["payload"]["deterministic_rejection_details"][
                0
            ]["candidate_id"],
            "finding_id": "F-"
            + verification["payload"]["deterministic_rejection_details"][0][
                "candidate_id"
            ][:12].upper(),
            "rule": "evidence_context_missing",
            "evidence_role": "verifier",
            "evidence_index": 0,
            "retrieval_source": "",
            "file": "pkg/service.py",
            "line": 11,
            "end_line": None,
            "field": "verified_evidence",
            "revised_issue": False,
            "message": "The cited verifier evidence was not retained in candidate context.",
        }
    ]


def test_finding_verifier_parses_forced_structured_tool_result() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]

    class FakeClient:
        default_config = ModelConfig(model="verifier-model")

        def __init__(self) -> None:
            self.calls = []

        async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def]
            self.calls.append((messages, config, tools))
            payload = {
                "results": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "status": "accepted",
                        "reason_codes": ["verified"],
                        "rationale": "The changed lookup executes before population.",
                        "verified_evidence": ["pkg/service.py:12"],
                    }
                ]
            }
            return ModelResponse(
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_finding_verification",
                            "arguments": json.dumps(payload),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=42),
                model="verifier-model",
                finish_reason="tool_calls",
            )

    client = FakeClient()
    verifier = module.FindingVerifier(client)

    batch = asyncio.run(
        verifier.verify(
            [candidate],
            ReviewRequest(
                repo_path=".",
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                    "@@ -11,0 +12,1 @@\n+return cache[key]\n"
                ),
            ),
            ContextState(goal="review pull request"),
        )
    )

    assert batch.results[0].status == "accepted"
    assert verifier.last_call_tokens == 42
    assert client.calls[0][1].temperature == 0.0
    assert (
        client.calls[0][1].tool_choice["function"]["name"]
        == "submit_finding_verification"
    )
    assert client.calls[0][2][0]["function"]["name"] == "submit_finding_verification"


def test_validate_verification_accepts_range_intersecting_changed_line() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The cited range contains the changed line.",
                verified_evidence=["pkg/service.py:10-12"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate],
        batch,
        ReviewRequest(
            repo_path=".",
            diff_mode=True,
            diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n"
                "+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n"
                "+return cache[key]\n"
            ),
        ),
    )

    assert result.results[0].status == "accepted"


def test_validate_verification_rejects_range_without_changed_line_intersection() -> (
    None
):
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The cited range misses the changed line.",
                verified_evidence=["pkg/service.py:1-10"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate],
        batch,
        ReviewRequest(
            repo_path=".",
            diff_mode=True,
            diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n"
                "+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n"
                "+return cache[key]\n"
            ),
        ),
    )

    assert result.results[0].status == "rejected"


def test_validate_verification_accepts_retained_hunk_context_line() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[_issue()]), iteration=0
    )[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The unchanged guard in the retained hunk confirms the ordering.",
                verified_evidence=["pkg/service.py:11"],
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,2 +11,3 @@\n guard = ready\n+return cache[key]\n cleanup()\n"
        ),
    )
    context = [
        {
            "candidate_id": candidate.candidate_id,
            "diff_hunks": [
                {
                    "path": "pkg/service.py",
                    "new_start": 11,
                    "new_count": 3,
                }
            ],
        }
    ]

    result, stats = module.validate_verifications_with_stats(
        [candidate], batch, request, candidate_context=context
    )

    assert result.results[0].status == "accepted"
    assert stats.checked_count == 1
    assert stats.passed_count == 1
    assert stats.rejected_count == 0


def test_validate_verification_accepts_retained_cross_file_symbol_reference() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[_issue()]), iteration=0
    )[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The retained caller demonstrates the affected path.",
                verified_evidence=["pkg/service.py:12", "pkg/caller.py:8"],
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = [
        {
            "candidate_id": candidate.candidate_id,
            "symbol_contexts": [
                {
                    "definitions": [],
                    "references": [{"path": "pkg/caller.py", "line": 8}],
                    "enclosing_symbols": [],
                }
            ],
        }
    ]

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert result.results[0].status == "accepted"


def test_validate_verification_accepts_retained_enclosing_symbol_line() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[_issue()]), iteration=0
    )[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The enclosing function guard supports the finding.",
                verified_evidence=["pkg/service.py:8"],
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = [
        {
            "candidate_id": candidate.candidate_id,
            "enclosing_symbols": [
                {"path": "pkg/service.py", "line": 8, "end_line": 16}
            ],
        }
    ]

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert result.results[0].status == "accepted"


def test_context_removed_by_budget_cannot_validate_evidence() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[_issue()]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [
            {
                "tool_name": "read_file",
                "arguments": {"file_path": "pkg/service.py"},
                "data": {
                    "file_path": "pkg/service.py",
                    "start_line": 1,
                    "line_count": 20,
                    "content": "x" * 4_000,
                },
            }
        ],
        max_chars=800,
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The cited line was clipped from the candidate payload.",
                verified_evidence=["pkg/service.py:13"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert context[0]["file_windows"] == []
    assert result.results[0].status == "rejected"


def test_candidate_context_retains_all_finding_cited_diff_hunks() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.impact = "The later grouping call receives the incompatible cache value."
    issue.impact_evidence = [
        issue.cause_evidence[0].model_copy(
            update={
                "line": 32,
                "end_line": 32,
                "statement": "The second changed hunk consumes the returned value.",
            }
        )
    ]
    issue.related_locations = [
        RelatedLocation(
            file="pkg/service.py",
            line=32,
            role="related",
            description="The downstream changed use participates in the same repair unit.",
        )
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
            "@@ -31,0 +32,1 @@\n+group(cache_value)\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate], request, [], max_chars=8_000
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="Both changed hunks support the finding.",
                verified_evidence=["pkg/service.py:12", "pkg/service.py:32"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert [item["new_start"] for item in context[0]["diff_hunks"]] == [12, 32]
    assert result.results[0].status == "accepted"


def test_candidate_context_retains_explicit_nonoverlapping_read_window() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.impact = "A later unchanged consumer exposes the compatibility break."
    issue.impact_evidence = [
        issue.cause_evidence[0].model_copy(
            update={
                "retrieval_source": "read_file",
                "line": 40,
                "end_line": 40,
                "statement": "The later consumer passes the stale value to callers.",
            }
        )
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [
            {
                "tool_name": "read_file",
                "arguments": {"file_path": "pkg/service.py"},
                "data": {
                    "file_path": "pkg/service.py",
                    "start_line": 38,
                    "line_count": 5,
                    "content": "38: prepare()\n40: publish(cache_value)\n42: cleanup()",
                },
            }
        ],
        max_chars=8_000,
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The retained read confirms the downstream impact.",
                verified_evidence=["pkg/service.py:12", "pkg/service.py:40"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert context[0]["file_windows"] == [
        {
            "path": "pkg/service.py",
            "start_line": 38,
            "end_line": 42,
            "content": "38: prepare()\n40: publish(cache_value)\n42: cleanup()",
            "truncated": False,
            "source": "read_file",
        }
    ]
    assert result.results[0].status == "accepted"


def test_candidate_context_retains_unchanged_primary_display_from_tool_read() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.location = "pkg/consumer.py:8"
    issue.primary_anchor = SourceAnchor(file="pkg/consumer.py", line=8)
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [
            {
                "tool_name": "read_file",
                "arguments": {"file_path": "pkg/consumer.py"},
                "data": {
                    "file_path": "pkg/consumer.py",
                    "start_line": 6,
                    "line_count": 5,
                    "content": "8: return consume(value)",
                },
            }
        ],
        max_chars=8_000,
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The changed cause and retained consumer establish the issue.",
                verified_evidence=["pkg/service.py:12", "pkg/consumer.py:8"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert context[0]["file_windows"][0]["path"] == "pkg/consumer.py"
    assert result.results[0].status == "accepted"


def test_candidate_context_does_not_invent_unread_evidence_location() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.impact = "An alleged downstream consumer exposes the break."
    issue.impact_evidence = [
        issue.cause_evidence[0].model_copy(
            update={
                "retrieval_source": "read_file",
                "file": "pkg/missing.py",
                "line": 40,
                "end_line": 40,
                "statement": "An unobserved consumer allegedly publishes the value.",
            }
        )
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate], request, [], max_chars=8_000
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The alleged consumer supports the impact.",
                verified_evidence=["pkg/service.py:12", "pkg/missing.py:40"],
            )
        ]
    )

    result, stats = module.validate_verifications_with_stats(
        [candidate], batch, request, candidate_context=context
    )

    assert context[0]["file_windows"] == []
    assert context[0]["symbol_contexts"] == []
    assert result.results[0].status == "rejected"
    assert any(
        item.rule == "tool_evidence_context_missing"
        and item.evidence_role == "impact"
        and item.file == "pkg/missing.py"
        and item.line == 40
        for item in stats.rejection_details
    )


def test_candidate_context_retains_explicit_symbol_context() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.trigger = "The retained caller reaches the changed lookup."
    issue.trigger_evidence = [
        issue.cause_evidence[0].model_copy(
            update={
                "retrieval_source": "find_symbol_context",
                "file": "pkg/caller.py",
                "line": 8,
                "end_line": 8,
                "statement": "The caller invokes the changed lookup.",
            }
        )
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [
            {
                "tool_name": "find_symbol_context",
                "arguments": {
                    "symbol": "load",
                    "path": "pkg/caller.py",
                },
                "data": {
                    "symbol": "load",
                    "backend": "python_ast",
                    "language": "python",
                    "definitions": [],
                    "references": [
                        {
                            "path": "pkg/caller.py",
                            "line": 8,
                            "line_text": "return load(key)",
                            "context": "8: return load(key)",
                        }
                    ],
                    "enclosing_symbols": [],
                },
            }
        ],
        max_chars=8_000,
    )
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The retained symbol reference confirms the trigger.",
                verified_evidence=["pkg/service.py:12", "pkg/caller.py:8"],
            )
        ]
    )

    result = module.validate_verifications(
        [candidate], batch, request, candidate_context=context
    )

    assert context[0]["symbol_contexts"][0]["references"][0]["line"] == 8
    assert result.results[0].status == "accepted"


def test_candidate_locations_precede_redundant_source_copies_under_budget() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.impact = "A later changed consumer exposes the compatibility break."
    issue.impact_evidence = [
        issue.cause_evidence[0].model_copy(
            update={
                "line": 32,
                "end_line": 32,
                "statement": "The later changed consumer publishes the stale value.",
            }
        )
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
            "@@ -31,0 +32,1 @@\n+publish(cache_value)\n"
        ),
    )
    direct_context = context_module.build_candidate_verifier_context(
        [candidate], request, [], max_chars=8_000
    )[0]
    direct_budget = len(json.dumps(direct_context, ensure_ascii=True))
    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [
            {
                "tool_name": "read_file",
                "arguments": {"file_path": "pkg/service.py"},
                "data": {
                    "file_path": "pkg/service.py",
                    "start_line": 8,
                    "line_count": 10,
                    "content": "x" * 4_000,
                },
            }
        ],
        max_chars=direct_budget,
    )[0]

    assert [item["new_start"] for item in context["diff_hunks"]] == [12, 32]
    assert context["file_windows"] == []


def test_candidate_context_orders_locations_by_evidence_role_priority() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    issue.cause_evidence[0].line = 20
    issue.contract_evidence[0].line = 30
    issue.trigger = "A caller supplies the stale key."
    issue.trigger_evidence = [
        issue.cause_evidence[0].model_copy(
            update={"line": 40, "statement": "The caller supplies the key."}
        )
    ]
    issue.impact = "The result is published."
    issue.impact_evidence = [
        issue.cause_evidence[0].model_copy(
            update={"line": 50, "statement": "The stale result is published."}
        )
    ]
    issue.related_locations = [
        RelatedLocation(file="pkg/service.py", line=60, role="related")
    ]
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]

    requested = context_module._candidate_evidence_locations(candidate)  # noqa: SLF001

    assert [item.role for item in requested] == [
        "primary",
        "cause",
        "contract",
        "trigger",
        "impact",
        "related",
    ]
    assert [item.start_line for item in requested] == [12, 20, 30, 40, 50, 60]
    assert [item.retrieval_source for item in requested] == [
        "",
        "git_diff",
        "git_diff",
        "git_diff",
        "git_diff",
        "",
    ]


def test_candidate_context_budget_trims_lower_priority_locations_first() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")

    def issue_through_contract() -> ReviewIssue:
        issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
        issue.cause_evidence[0].line = 20
        issue.contract_evidence[0].line = 30
        return issue

    full_issue = issue_through_contract()
    full_issue.trigger = "A caller supplies the stale key."
    full_issue.trigger_evidence = [
        full_issue.cause_evidence[0].model_copy(
            update={"line": 40, "statement": "The caller supplies the key."}
        )
    ]
    full_issue.impact = "The result is published."
    full_issue.impact_evidence = [
        full_issue.cause_evidence[0].model_copy(
            update={"line": 50, "statement": "The stale result is published."}
        )
    ]
    full_issue.related_locations = [
        RelatedLocation(file="pkg/service.py", line=60, role="related")
    ]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+primary_change()\n"
            "@@ -19,0 +20,1 @@\n+cause_change()\n"
            "@@ -29,0 +30,1 @@\n+contract_change()\n"
            "@@ -39,0 +40,1 @@\n+trigger_change()\n"
            "@@ -49,0 +50,1 @@\n+impact_change()\n"
            "@@ -59,0 +60,1 @@\n+related_change()\n"
        ),
    )
    contract_candidate = module.build_candidates(
        ReviewReport(issues=[issue_through_contract()]), iteration=0
    )[0]
    contract_context = context_module.build_candidate_verifier_context(
        [contract_candidate], request, [], max_chars=8_000
    )[0]
    exact_contract_budget = len(json.dumps(contract_context, ensure_ascii=True))
    full_candidate = module.build_candidates(
        ReviewReport(issues=[full_issue]), iteration=0
    )[0]

    bounded = context_module.build_candidate_verifier_context(
        [full_candidate],
        request,
        [],
        max_chars=exact_contract_budget,
    )[0]

    assert [item["new_start"] for item in bounded["diff_hunks"]] == [12, 20, 30]


def test_candidate_context_keeps_direct_diff_before_large_manifest_span() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    context_module = importlib.import_module("src.analyzer.verifier_context")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    manifest = {
        "candidate_id": "C-graph",
        "changed_anchor": {
            "file": "pkg/service.py",
            "line": 12,
            "end_line": 12,
        },
        "included_spans": [
            {
                "file": "pkg/service.py",
                "start_line": 1,
                "end_line": 80,
                "content": "x" * 4_000,
                "context_hash": "manifest-hash",
            }
        ],
        "included_graph_paths": [],
        "excluded_low_confidence_paths": [],
    }

    context = context_module.build_candidate_verifier_context(
        [candidate],
        request,
        [],
        max_chars=1_200,
        context_manifests=[manifest],
        context_mode="graph_hybrid",
    )[0]

    assert [item["new_start"] for item in context["diff_hunks"]] == [12]
    assert context["included_spans"] == []


def test_validate_verification_rejects_unretained_or_unanchored_context() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[_issue()]), iteration=0
    )[0]
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The cited line was not retained for this candidate.",
                verified_evidence=["pkg/unrelated.py:8"],
            )
        ]
    )
    changed_request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )
    unanchored_request = changed_request.model_copy(
        update={"diff_text": changed_request.diff_text.replace("+12,1", "+13,1")}
    )
    context = [
        {
            "candidate_id": candidate.candidate_id,
            "file_windows": [
                {"path": "pkg/unrelated.py", "start_line": 8, "end_line": 8}
            ],
        }
    ]

    unretained = module.validate_verifications([candidate], batch, changed_request)
    unanchored, unanchored_stats = module.validate_verifications_with_stats(
        [candidate], batch, unanchored_request, candidate_context=context
    )

    assert unretained.results[0].reason_codes == ["deterministic_evidence_invalid"]
    assert unanchored.results[0].reason_codes == ["deterministic_evidence_invalid"]
    assert any(
        item.rule == "finding_primary_location_invalid"
        and item.file == "pkg/service.py"
        and item.line == 12
        for item in unanchored_stats.rejection_details
    )


@pytest.mark.parametrize(
    ("mutate_issue", "context", "expected_rule", "expected_field"),
    [
        (
            lambda issue: setattr(issue.cause_evidence[0], "line", None),
            {"diff_hunks": []},
            "evidence_location_missing",
            "file/line",
        ),
        (
            lambda issue: setattr(issue.cause_evidence[0], "line", 30),
            {"diff_hunks": []},
            "diff_evidence_context_missing",
            "retrieval_source/file/line",
        ),
        (
            lambda issue: (
                setattr(issue.cause_evidence[0], "retrieval_source", "read_file"),
                setattr(issue.cause_evidence[0], "line", 30),
            ),
            {"file_windows": []},
            "tool_evidence_context_missing",
            "retrieval_source/file/line",
        ),
    ],
)
def test_deterministic_rejection_details_name_failed_evidence_rule(
    mutate_issue,
    context,
    expected_rule: str,
    expected_field: str,
) -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    mutate_issue(issue)
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    candidate_context = {
        "candidate_id": candidate.candidate_id,
        "context_mode": "graph_hybrid",
        "evidence_policy": {
            "require_manifest": False,
            "allow_diff_evidence": True,
            "allow_tool_evidence": True,
            "allow_manifest_evidence": True,
            "require_context_hash_for_manifest": True,
        },
        **context,
    }
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The finding is supported.",
                verified_evidence=["pkg/service.py:12"],
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )

    result, stats = module.validate_verifications_with_stats(
        [candidate], batch, request, candidate_context=[candidate_context]
    )

    assert result.results[0].status == "rejected"
    detail = next(
        item for item in stats.rejection_details if item.rule == expected_rule
    )
    assert detail.candidate_id == candidate.candidate_id
    assert detail.finding_id == issue.finding_id
    assert detail.evidence_role == "cause"
    assert detail.evidence_index == 0
    assert detail.file == "pkg/service.py"
    assert detail.field == expected_field


def test_deterministic_rejection_details_distinguish_manifest_id_and_hash() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    from src.analyzer.finding_schema import context_hash

    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )

    def rejection_rules(issue: ReviewIssue, context: dict[str, object]) -> set[str]:
        candidate = module.build_candidates(
            ReviewReport(summary="review", issues=[issue]), iteration=0
        )[0]
        batch = schemas.FindingVerificationBatch(
            results=[
                schemas.FindingVerification(
                    candidate_id=candidate.candidate_id,
                    status="accepted",
                    reason_codes=["verified"],
                    rationale="The finding is supported.",
                    verified_evidence=["pkg/service.py:12"],
                )
            ]
        )
        _, stats = module.validate_verifications_with_stats(
            [candidate],
            batch,
            request,
            candidate_context=[{"candidate_id": candidate.candidate_id, **context}],
        )
        return {item.rule for item in stats.rejection_details}

    id_issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    id_issue.context_manifest_id = "C-expected"
    for evidence in id_issue.all_evidence():
        evidence.context_manifest_id = "C-other"
        evidence.context_hash = context_hash("12: return cache[key]")
    id_rules = rejection_rules(
        id_issue,
        {
            "context_manifest_id": "C-expected",
            "included_spans": [],
            "context_mode": "graph_hybrid",
        },
    )

    hash_issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    hash_issue.context_manifest_id = "C-expected"
    for evidence in hash_issue.all_evidence():
        evidence.context_manifest_id = "C-expected"
        evidence.context_hash = context_hash("wrong content")
    hash_rules = rejection_rules(
        hash_issue,
        {
            "context_manifest_id": "C-expected",
            "included_spans": [
                {
                    "file": "pkg/service.py",
                    "start_line": 12,
                    "end_line": 12,
                    "context_hash": context_hash("12: return cache[key]"),
                }
            ],
            "context_mode": "graph_hybrid",
        },
    )

    assert "manifest_id_mismatch" in id_rules
    assert "manifest_hash_mismatch" in hash_rules


def test_revised_finding_failure_is_marked_after_full_revalidation() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    schemas = importlib.import_module("src.analyzer.schemas")
    issue = _structured_boundary_issue(Severity.WARNING, confidence=0.92)
    candidate = module.build_candidates(
        ReviewReport(summary="review", issues=[issue]), iteration=0
    )[0]
    revised = candidate.issue.model_copy(deep=True)
    revised.cause_evidence[0].line = 30
    batch = schemas.FindingVerificationBatch(
        results=[
            schemas.FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The narrower issue is supported.",
                verified_evidence=["pkg/service.py:12"],
                revised_issue=revised,
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )

    result, stats = module.validate_verifications_with_stats(
        [candidate],
        batch,
        request,
        candidate_context=[
            {
                "candidate_id": candidate.candidate_id,
                "context_mode": "graph_hybrid",
                "diff_hunks": [],
            }
        ],
    )

    assert result.results[0].status == "rejected"
    assert {item.rule for item in stats.rejection_details} >= {
        "diff_evidence_context_missing",
        "revised_evidence_invalid",
    }
    assert all(item.revised_issue for item in stats.rejection_details)


def test_finding_verifier_injects_candidate_scoped_tool_evidence() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]

    class FakeClient:
        default_config = ModelConfig(model="verifier-model")

        def __init__(self) -> None:
            self.payload: dict[str, object] = {}

        async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def]
            self.payload = json.loads(messages[1].content)
            return ModelResponse(
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_finding_verification",
                            "arguments": json.dumps(
                                {
                                    "results": [
                                        {
                                            "candidate_id": candidate.candidate_id,
                                            "status": "accepted",
                                            "reason_codes": ["verified"],
                                            "rationale": "The window confirms the lookup order.",
                                            "verified_evidence": ["pkg/service.py:12"],
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=42),
                model="verifier-model",
                finish_reason="tool_calls",
            )

    client = FakeClient()
    verifier = module.FindingVerifier(client)
    diff = (
        "diff --git a/pkg/service.py b/pkg/service.py\n"
        "--- a/pkg/service.py\n"
        "+++ b/pkg/service.py\n"
        "@@ -11,0 +12,1 @@\n"
        "+return cache[key]\n"
    )
    tool_evidence = [
        {
            "tool_name": "get_changed_context",
            "arguments": {"file_path": "pkg/service.py", "line": 12},
            "data": {
                "file_path": "pkg/service.py",
                "hunk": {
                    "index": 0,
                    "header": "@@ -11,0 +12,1 @@",
                    "changed_new_lines": [12],
                    "text": "@@ -11,0 +12,1 @@\n+return cache[key]",
                },
                "file_window": {
                    "start_line": 8,
                    "end_line": 16,
                    "content": "12: return cache[key]",
                },
                "enclosing_symbols": [
                    {
                        "path": "pkg/service.py",
                        "line": 8,
                        "end_line": 16,
                        "kind": "function",
                        "name": "load",
                        "signature": "def load(key):",
                        "confidence": 1.0,
                    }
                ],
            },
        },
        {
            "tool_name": "read_file",
            "arguments": {"file_path": "pkg/unrelated.py", "offset": 0, "limit": 20},
            "data": {
                "file_path": "pkg/unrelated.py",
                "start_line": 1,
                "line_count": 1,
                "content": "1: unrelated = True",
            },
        },
    ]

    result = asyncio.run(
        verifier.verify(
            [candidate],
            ReviewRequest(repo_path=".", diff_mode=True, diff_text=diff),
            ContextState(goal="review pull request"),
            tool_evidence=tool_evidence,
        )
    )

    assert result.results[0].status == "accepted"
    candidate_context = client.payload["candidate_context"]
    assert isinstance(candidate_context, list)
    assert candidate_context[0]["candidate_id"] == candidate.candidate_id
    assert candidate_context[0]["diff_hunks"][0]["changed_new_lines"] == [12]
    assert candidate_context[0]["file_windows"][0]["content"] == "12: return cache[key]"
    assert candidate_context[0]["enclosing_symbols"][0]["name"] == "load"
    assert "unrelated.py" not in json.dumps(candidate_context)


def test_deterministic_evidence_rejection_is_distinct_from_model_verdict() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(summary="review", issues=[_issue()])
    candidate = module.build_candidates(report, iteration=0)[0]

    class FakeClient:
        default_config = ModelConfig(model="verifier-model")

        async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def]
            return ModelResponse(
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_finding_verification",
                            "arguments": json.dumps(
                                {
                                    "results": [
                                        {
                                            "candidate_id": candidate.candidate_id,
                                            "status": "accepted",
                                            "reason_codes": ["verified"],
                                            "rationale": "The claim is supported.",
                                            "verified_evidence": ["pkg/service.py:1"],
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=42),
                model="verifier-model",
                finish_reason="tool_calls",
            )

    verifier = module.FindingVerifier(FakeClient())
    result = asyncio.run(
        verifier.verify(
            [candidate],
            ReviewRequest(
                repo_path=".",
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n"
                    "+++ b/pkg/service.py\n"
                    "@@ -11,0 +12,1 @@\n"
                    "+return cache[key]\n"
                ),
            ),
            ContextState(goal="review pull request"),
        )
    )

    assert verifier.last_raw_batch.results[0].status == "accepted"
    assert verifier.last_raw_batch.results[0].reason_codes == ["verified"]
    assert result.results[0].status == "rejected"
    assert result.results[0].reason_codes == ["deterministic_evidence_invalid"]
    assert verifier.last_post_validation_batch == result


def test_high_confidence_changed_line_concrete_risk_promotes_info() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(
        summary="review",
        issues=[
            ReviewIssue(
                severity=Severity.INFO,
                location="pkg/service.py:12",
                evidence="The changed fallback silently drops user data.",
                suggestion="Preserve the value to avoid this data loss regression.",
                confidence=0.9,
            )
        ],
    )

    reviewed = module.review_candidate_severities(
        report,
        ReviewRequest(
            repo_path=".",
            diff_mode=True,
            diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n"
                "+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n"
                "+return cache[key]\n"
            ),
        ),
    )

    assert reviewed.report.issues[0].severity == Severity.WARNING
    assert reviewed.high_confidence_info_count == 1
    assert reviewed.reviewed_count == 1
    assert reviewed.promoted_count == 1


def test_severity_review_uses_structured_cause_anchor_for_unchanged_display() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    issue = _structured_boundary_issue(Severity.INFO, confidence=0.9)
    issue.location = "pkg/consumer.py:8"
    issue.primary_anchor = SourceAnchor(file="pkg/consumer.py", line=8)
    report = ReviewReport(summary="review", issues=[issue])

    reviewed = module.review_candidate_severities(
        report,
        ReviewRequest(
            repo_path=".",
            diff_mode=True,
            diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n"
                "+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n"
                "+return cache[key]\n"
            ),
        ),
    )

    assert reviewed.report.issues[0].severity == Severity.WARNING
    assert reviewed.reviewed_count == 1
    assert reviewed.promoted_count == 1


def test_severity_review_keeps_speculative_unchanged_and_style_findings() -> None:
    module = importlib.import_module("src.analyzer.finding_verifier")
    report = ReviewReport(
        summary="review",
        issues=[
            ReviewIssue(
                severity=Severity.INFO,
                location="pkg/service.py:12",
                evidence="This could perhaps be simplified.",
                suggestion="Consider a helper.",
                confidence=0.95,
            ),
            ReviewIssue(
                severity=Severity.INFO,
                location="pkg/service.py:3",
                evidence="This silently drops user data.",
                suggestion="Avoid data loss.",
                confidence=0.95,
            ),
            ReviewIssue(
                severity=Severity.STYLE,
                location="pkg/service.py:12",
                evidence="This data loss wording is only a naming example.",
                suggestion="Rename the local variable.",
                confidence=0.95,
            ),
        ],
    )

    reviewed = module.review_candidate_severities(
        report,
        ReviewRequest(
            repo_path=".",
            diff_mode=True,
            diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n"
                "+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n"
                "+return cache[key]\n"
            ),
        ),
    )

    assert [item.severity for item in reviewed.report.issues] == [
        Severity.INFO,
        Severity.INFO,
        Severity.STYLE,
    ]
    assert reviewed.high_confidence_info_count == 2
    assert reviewed.reviewed_count == 1
    assert reviewed.promoted_count == 0


def test_orchestrator_passes_successful_context_tool_results_to_verifier(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")
    changed = tmp_path / "pkg" / "service.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("return cache[key]\n", encoding="utf-8")

    class CapturingVerifier:
        def __init__(self) -> None:
            self.tool_evidence: list[dict[str, object]] | None = None

        async def verify(  # type: ignore[no-untyped-def]
            self, candidates, request, state, *, tool_evidence=None
        ):
            self.tool_evidence = tool_evidence
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status="accepted",
                        reason_codes=["verified"],
                        rationale="The changed context confirms the lookup order.",
                        verified_evidence=["pkg/service.py:1"],
                    )
                    for item in candidates
                ]
            )

    verifier = CapturingVerifier()
    orchestrator = AgentOrchestrator(
        finding_verifier=verifier,
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
        review_diff_first_changed_files=False,
        review_max_iterations=2,
    )
    calls = 0

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return AnalysisPlan(
                needs_tools=True,
                tool_calls=[
                    {
                        "id": "changed-context",
                        "type": "function",
                        "function": {
                            "name": "get_changed_context",
                            "arguments": json.dumps(
                                {"file_path": "pkg/service.py", "line": 1, "radius": 2}
                            ),
                        },
                    }
                ],
            )
        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="review",
                issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                        location="pkg/service.py:1",
                        evidence="`return cache[key]` executes before the key is populated",
                        suggestion="Populate the cache before reading it.",
                        confidence=0.92,
                    )
                ],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n"
                    "+++ b/pkg/service.py\n"
                    "@@ -0,0 +1,1 @@\n"
                    "+return cache[key]\n"
                ),
            )
        )
    )

    assert len(response.report.issues) == 1
    assert verifier.tool_evidence is not None
    assert verifier.tool_evidence[0]["tool_name"] == "get_changed_context"
    assert verifier.tool_evidence[0]["data"]["hunk"]["changed_new_lines"] == [1]


def test_orchestrator_retries_needs_evidence_once_before_accepting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schemas = importlib.import_module("src.analyzer.schemas")

    class RepairingVerifier:
        def __init__(self) -> None:
            self.calls = 0

        async def verify(self, candidates, request, state):  # type: ignore[no-untyped-def]
            self.calls += 1
            status = "needs_evidence" if self.calls == 1 else "accepted"
            reason = "cross_file_context_missing" if self.calls == 1 else "verified"
            return schemas.FindingVerificationBatch(
                results=[
                    schemas.FindingVerification(
                        candidate_id=item.candidate_id,
                        status=status,
                        reason_codes=[reason],
                        rationale="Need the caller context."
                        if self.calls == 1
                        else "Caller confirms the path.",
                        verified_evidence=["pkg/service.py:12"]
                        if self.calls > 1
                        else [],
                    )
                    for item in candidates
                ]
            )

    verifier = RepairingVerifier()
    orchestrator = AgentOrchestrator(
        finding_verifier=verifier,
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            draft_review=ReviewReport(summary="review", issues=[_issue()])
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/pkg/service.py b/pkg/service.py\n"
                    "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                    "@@ -11,0 +12,1 @@\n+return cache[key]\n"
                ),
            )
        )
    )

    assert verifier.calls == 2
    assert len(response.report.issues) == 1
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    repair = next(
        event
        for event in events
        if event["event_type"] == "finding_evidence_repair_completed"
    )
    assert repair["payload"]["round"] == 1
