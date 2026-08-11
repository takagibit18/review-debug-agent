"""Tests for semantic finding verification and review-loop enforcement."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from src.analyzer.finding_schema import EvidenceProvenance, RepairIntent, SourceAnchor
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
    unanchored = module.validate_verifications(
        [candidate], batch, unanchored_request, candidate_context=context
    )

    assert unretained.results[0].reason_codes == ["deterministic_evidence_invalid"]
    assert unanchored.results[0].reason_codes == ["deterministic_evidence_invalid"]


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
