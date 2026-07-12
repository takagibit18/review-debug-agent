"""Tests for semantic finding verification and review-loop enforcement."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

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
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "finding_candidates_built" for event in events)
    assert any(event["event_type"] == "finding_verification_completed" for event in events)


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
            ReviewRequest(repo_path=".", diff_mode=True, diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n+return cache[key]\n"
            )),
            ContextState(goal="review pull request"),
        )
    )

    assert batch.results[0].status == "accepted"
    assert verifier.last_call_tokens == 42
    assert client.calls[0][1].temperature == 0.0
    assert client.calls[0][1].tool_choice["function"]["name"] == "submit_finding_verification"
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


def test_validate_verification_rejects_range_without_changed_line_intersection() -> None:
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
                        rationale="Need the caller context." if self.calls == 1 else "Caller confirms the path.",
                        verified_evidence=["pkg/service.py:12"] if self.calls > 1 else [],
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
        return AnalysisPlan(draft_review=ReviewReport(summary="review", issues=[_issue()]))

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(
            repo_path=str(tmp_path), diff_mode=True, diff_text=(
                "diff --git a/pkg/service.py b/pkg/service.py\n"
                "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
                "@@ -11,0 +12,1 @@\n+return cache[key]\n"
            )
        ))
    )

    assert verifier.calls == 2
    assert len(response.report.issues) == 1
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    repair = next(
        event for event in events if event["event_type"] == "finding_evidence_repair_completed"
    )
    assert repair["payload"]["round"] == 1
