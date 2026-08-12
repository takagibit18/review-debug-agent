"""Trusted provenance binding tests for structured finding evidence."""

from __future__ import annotations

from src.analyzer.evidence_binding import bind_candidate_evidence
from src.analyzer.finding_schema import EvidenceProvenance, RepairIntent, SourceAnchor
from src.analyzer.finding_verifier import (
    build_candidates,
    validate_verifications,
    validate_verifications_with_stats,
)
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import (
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
)
from src.analyzer.verifier_context import build_candidate_verifier_context


def _request() -> ReviewRequest:
    return ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-return load(value)\n"
            "+return load(value + 1)\n"
        ),
    )


def _issue(
    *,
    contract_source: str = "git_diff",
    contract_file: str = "helper.py",
    contract_line: int = 5,
) -> ReviewIssue:
    return ReviewIssue(
        severity=Severity.WARNING,
        location="main.py:1",
        evidence="The caller and helper both increment the same input.",
        suggestion="Increment at exactly one side of the caller/helper boundary.",
        confidence=0.95,
        schema_version="2.0",
        finding_id="F-model",
        primary_anchor=SourceAnchor(file="main.py", line=1, symbol_id="main"),
        observed_behavior="The value is incremented twice.",
        causal_mechanism="The changed caller increments before a helper that increments.",
        violated_invariant="The input must be incremented exactly once.",
        repair_intent=RepairIntent(
            action="Remove one increment",
            targets=["main.load", "helper.load"],
            boundary="caller/helper contract",
        ),
        cause_evidence=[
            EvidenceProvenance(
                candidate_id="wrong-model-id",
                retrieval_source="git_diff",
                file="main.py",
                line=1,
                statement="The changed caller increments before invoking load.",
            )
        ],
        contract_evidence=[
            EvidenceProvenance(
                candidate_id="",
                retrieval_source=contract_source,
                file=contract_file,
                line=contract_line,
                statement="The helper already increments the received value.",
            )
        ],
    )


def _read_evidence(
    *,
    file: str = "helper.py",
    start_line: int = 3,
    line_count: int = 6,
) -> list[dict[str, object]]:
    return [
        {
            "tool_name": "read_file",
            "arguments": {"file_path": file},
            "data": {
                "file_path": file,
                "start_line": start_line,
                "line_count": line_count,
                "content": "3: def load(value):\n5:     return value + 1",
            },
        }
    ]


def _accepted(candidate_id: str) -> FindingVerificationBatch:
    return FindingVerificationBatch(
        results=[
            FindingVerification(
                candidate_id=candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The retained caller and helper code prove the double increment.",
                verified_evidence=["main.py:1", "helper.py:5"],
            )
        ]
    )


def test_read_evidence_mislabeled_as_diff_is_bound_to_successful_read() -> None:
    request = _request()
    tools = _read_evidence()
    candidates = build_candidates(
        ReviewReport(issues=[_issue(contract_source="git_diff")]), iteration=0
    )

    bound = bind_candidate_evidence(candidates, request, tools)
    context = build_candidate_verifier_context(bound, request, tools)
    result = validate_verifications(
        bound,
        _accepted(bound[0].candidate_id),
        request,
        candidate_context=context,
    )

    assert bound[0].issue.contract_evidence[0].retrieval_source == "read_file"
    assert result.results[0].status == "accepted"


def test_nonexistent_evidence_remains_fail_closed() -> None:
    request = _request()
    candidates = build_candidates(
        ReviewReport(
            issues=[
                _issue(
                    contract_source="git_diff",
                    contract_file="missing.py",
                    contract_line=20,
                )
            ]
        ),
        iteration=0,
    )

    bound = bind_candidate_evidence(candidates, request, [])
    context = build_candidate_verifier_context(bound, request, [])
    batch = _accepted(bound[0].candidate_id)
    batch.results[0].verified_evidence = ["main.py:1", "missing.py:20"]
    result = validate_verifications(bound, batch, request, candidate_context=context)

    assert bound[0].issue.contract_evidence[0].retrieval_source == "git_diff"
    assert result.results[0].status == "rejected"


def test_build_candidates_overwrites_empty_and_wrong_evidence_ids() -> None:
    report = ReviewReport(issues=[_issue()])

    candidate = build_candidates(report, iteration=0)[0]

    assert candidate.issue.candidate_id == candidate.candidate_id
    assert {item.candidate_id for item in candidate.issue.all_evidence()} == {
        candidate.candidate_id
    }
    assert {item.candidate_id for item in report.issues[0].all_evidence()} == {
        candidate.candidate_id
    }


def test_ambiguous_unclaimed_provenance_is_not_auto_bound() -> None:
    request = _request()
    issue = _issue(
        contract_source="reviewer_context",
        contract_file="main.py",
        contract_line=1,
    )
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    tools = _read_evidence(file="main.py", start_line=1, line_count=2)

    bound = bind_candidate_evidence(candidates, request, tools)
    context = build_candidate_verifier_context(bound, request, tools)
    result = validate_verifications(
        bound,
        FindingVerificationBatch(
            results=[
                FindingVerification(
                    candidate_id=bound[0].candidate_id,
                    status="accepted",
                    reason_codes=["verified"],
                    rationale="The location is present in two source representations.",
                    verified_evidence=["main.py:1"],
                )
            ]
        ),
        request,
        candidate_context=context,
    )

    assert bound[0].issue.contract_evidence[0].retrieval_source == "reviewer_context"
    assert result.results[0].status == "rejected"


def test_revised_finding_is_rebound_to_retained_trusted_context() -> None:
    request = _request()
    tools = _read_evidence()
    candidates = build_candidates(ReviewReport(issues=[_issue()]), iteration=0)
    bound = bind_candidate_evidence(candidates, request, tools)
    context = build_candidate_verifier_context(bound, request, tools)
    revised = bound[0].issue.model_copy(deep=True)
    revised.observed_behavior = "The supported caller path increments the value twice."
    revised.contract_evidence[0].candidate_id = "verifier-invented-id"
    revised.contract_evidence[0].retrieval_source = "git_diff"
    batch = _accepted(bound[0].candidate_id)
    batch.results[0].revised_issue = revised

    result = validate_verifications(bound, batch, request, candidate_context=context)

    assert result.results[0].status == "accepted"
    assert result.results[0].revised_issue is not None
    accepted = result.results[0].revised_issue
    assert accepted.contract_evidence[0].retrieval_source == "read_file"
    assert {item.candidate_id for item in accepted.all_evidence()} == {
        bound[0].candidate_id
    }


def test_revised_finding_new_unseen_location_is_rejected() -> None:
    request = _request()
    tools = _read_evidence()
    candidates = build_candidates(ReviewReport(issues=[_issue()]), iteration=0)
    bound = bind_candidate_evidence(candidates, request, tools)
    context = build_candidate_verifier_context(bound, request, tools)
    revised = bound[0].issue.model_copy(deep=True)
    revised.contract_evidence[0] = revised.contract_evidence[0].model_copy(
        update={
            "retrieval_source": "read_file",
            "file": "missing.py",
            "line": 99,
            "end_line": 99,
            "statement": "An unseen helper allegedly increments the value.",
        }
    )
    batch = _accepted(bound[0].candidate_id)
    batch.results[0].revised_issue = revised

    result, stats = validate_verifications_with_stats(
        bound, batch, request, candidate_context=context
    )

    assert result.results[0].status == "rejected"
    assert any(
        item.rule == "tool_evidence_context_missing"
        and item.evidence_role == "contract"
        and item.file == "missing.py"
        and item.line == 99
        and item.revised_issue
        for item in stats.rejection_details
    )
