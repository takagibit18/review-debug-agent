"""PR-causality tests independent from the issue display location."""

from __future__ import annotations

from src.analyzer.finding_schema import EvidenceProvenance, RepairIntent, SourceAnchor
from src.analyzer.finding_verifier import (
    build_candidates,
    validate_verifications_with_stats,
)
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import (
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
)


def _request() -> ReviewRequest:
    return ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n"
            "+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n"
            "+dispatch_changed_value()\n"
        ),
    )


def _issue(
    *,
    display_file: str,
    display_line: int,
    cause_file: str = "pkg/service.py",
    cause_line: int = 12,
    cause_source: str = "git_diff",
    contract_on_changed_line: bool = False,
) -> ReviewIssue:
    contract_file = "pkg/service.py" if contract_on_changed_line else display_file
    contract_line = 12 if contract_on_changed_line else display_line
    contract_source = "git_diff" if contract_on_changed_line else "read_file"
    return ReviewIssue(
        severity=Severity.WARNING,
        location=f"{display_file}:{display_line}",
        evidence="`consume(value)` now raises for the value dispatched by the changed caller.",
        suggestion="Preserve the caller/consumer value contract.",
        confidence=0.95,
        schema_version="2.0",
        finding_id="F-causal-anchor",
        primary_anchor=SourceAnchor(file=display_file, line=display_line),
        observed_behavior="The unchanged consumer now receives an invalid value.",
        causal_mechanism="The changed caller dispatches a value outside the consumer contract.",
        violated_invariant="The caller must dispatch values accepted by the consumer.",
        repair_intent=RepairIntent(
            action="Restore the dispatched value contract",
            targets=["service.dispatch", "consumer.consume"],
            boundary="caller/consumer value contract",
        ),
        cause_evidence=[
            EvidenceProvenance(
                retrieval_source=cause_source,
                file=cause_file,
                line=cause_line,
                statement="This caller now dispatches the incompatible value.",
            )
        ],
        contract_evidence=[
            EvidenceProvenance(
                retrieval_source=contract_source,
                file=contract_file,
                line=contract_line,
                statement="The consumer rejects values outside its established contract.",
            )
        ],
    )


def _validate(issue: ReviewIssue):  # type: ignore[no-untyped-def]
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    retained_files = {
        evidence.file
        for evidence in issue.all_evidence()
        if evidence.file != "pkg/service.py"
    }
    if (
        issue.primary_anchor is not None
        and issue.primary_anchor.file != "pkg/service.py"
    ):
        retained_files.add(issue.primary_anchor.file)
    context = {
        "candidate_id": candidate.candidate_id,
        "context_mode": "agent_search",
        "diff_hunks": [
            {
                "path": "pkg/service.py",
                "new_start": 12,
                "new_count": 1,
                "source": "diff",
            }
        ],
        "file_windows": [
            {
                "path": file,
                "start_line": 1,
                "end_line": 100,
                "source": "read_file",
            }
            for file in sorted(retained_files)
        ],
    }
    verified_locations = [
        {"file": evidence.file, "line": evidence.line}
        for evidence in issue.all_evidence()
        if evidence.line is not None
    ]
    batch = FindingVerificationBatch(
        results=[
            FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The retained caller and consumer establish the causal chain.",
                verified_evidence=verified_locations,
            )
        ]
    )
    return validate_verifications_with_stats(
        [candidate],
        batch,
        _request(),
        candidate_context=[context],
    )


def test_changed_display_location_with_changed_cause_anchor_passes() -> None:
    result, _ = _validate(
        _issue(
            display_file="pkg/service.py",
            display_line=12,
            contract_on_changed_line=True,
        )
    )

    assert result.results[0].status == "accepted"


def test_unchanged_display_location_with_changed_cause_anchor_passes() -> None:
    result, stats = _validate(_issue(display_file="pkg/consumer.py", display_line=8))

    assert result.results[0].status == "accepted"
    assert not any(
        detail.rule == "finding_primary_location_invalid"
        for detail in stats.rejection_details
    )


def test_unchanged_display_without_changed_cause_evidence_fails_closed() -> None:
    result, stats = _validate(
        _issue(
            display_file="pkg/consumer.py",
            display_line=8,
            cause_file="pkg/consumer.py",
            cause_line=8,
            cause_source="read_file",
            contract_on_changed_line=True,
        )
    )

    assert result.results[0].status == "rejected"
    assert any(
        detail.rule == "pr_causal_anchor_missing" and detail.evidence_role == "cause"
        for detail in stats.rejection_details
    )


def test_unchanged_downstream_symptom_is_not_rejected_for_its_display_site() -> None:
    result, _ = _validate(_issue(display_file="pkg/downstream.py", display_line=40))

    assert result.results[0].status == "accepted"
