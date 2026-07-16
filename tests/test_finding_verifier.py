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
