"""Trusted provenance binding tests for structured finding evidence."""

from __future__ import annotations

from src.analyzer.evidence_binding import bind_candidate_evidence
from src.analyzer.finding_integrity import build_candidates
from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RepairIntent,
    SourceAnchor,
)
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewRequest
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


def _context(
    candidates,
    request: ReviewRequest,
    tools: list[dict[str, object]],
    manifests: list[dict[str, object]] | None = None,
):
    bound = bind_candidate_evidence(
        candidates,
        request,
        tools,
        context_manifests=manifests,
    )
    return bound, build_candidate_verifier_context(
        bound,
        request,
        tools,
        context_manifests=manifests,
    )


def test_read_evidence_mislabeled_as_diff_is_bound_to_successful_read() -> None:
    request = _request()
    candidates = build_candidates(
        ReviewReport(issues=[_issue(contract_source="git_diff")]), iteration=0
    )

    bound, _ = _context(candidates, request, _read_evidence())

    assert bound[0].issue.contract_evidence[0].retrieval_source == "read_file"


def test_omitted_source_is_bound_from_successful_read() -> None:
    request = _request()
    candidates = build_candidates(
        ReviewReport(issues=[_issue(contract_source="")]), iteration=0
    )

    bound, _ = _context(candidates, request, _read_evidence())

    assert bound[0].issue.contract_evidence[0].retrieval_source == "read_file"


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

    bound, context = _context(candidates, request, [])

    assert bound[0].issue.contract_evidence[0].retrieval_source == "git_diff"
    assert not context[0]["included_spans"]
    assert not context[0]["file_windows"]


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


def test_diff_and_read_location_uses_system_priority_not_model_label() -> None:
    request = _request()
    issue = _issue(
        contract_source="model_invented_source",
        contract_file="main.py",
        contract_line=1,
    )
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    tools = _read_evidence(file="main.py", start_line=1, line_count=2)

    bound, _ = _context(candidates, request, tools)

    assert bound[0].issue.contract_evidence[0].retrieval_source == "git_diff"


def test_read_precedes_other_tool_representations_without_diff() -> None:
    request = _request()
    issue = _issue(
        contract_source="model_invented_source",
        contract_file="helper.py",
        contract_line=5,
    )
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    tools = [
        *_read_evidence(),
        {
            "tool_name": "get_changed_context",
            "arguments": {"file_path": "helper.py", "line": 5},
            "data": {
                "file_path": "helper.py",
                "hunk": {
                    "path": "helper.py",
                    "start_line": 5,
                    "end_line": 5,
                },
                "enclosing_symbols": [],
            },
        },
    ]

    bound, _ = _context(candidates, request, tools)

    assert bound[0].issue.contract_evidence[0].retrieval_source == "read_file"


def test_explicit_fake_manifest_is_not_rewritten_as_diff_or_read() -> None:
    request = _request()
    issue = _issue(
        contract_source="relation_graph",
        contract_file="main.py",
        contract_line=1,
    )
    issue.contract_evidence[0].context_manifest_id = "C-FAKE"
    issue.contract_evidence[0].context_hash = "fake-hash"
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    bound, _ = _context(candidates, request, _read_evidence(file="main.py"))

    evidence = bound[0].issue.contract_evidence[0]
    assert evidence.context_manifest_id == "C-FAKE"
    assert evidence.context_hash == "fake-hash"
    assert evidence.retrieval_source == "relation_graph"


def test_ambiguous_manifest_only_source_remains_fail_closed() -> None:
    request = _request()
    issue = _issue(
        contract_source="reviewer_context",
        contract_file="helper.py",
        contract_line=5,
    )
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    manifests = [
        {
            "candidate_id": manifest_id,
            "changed_anchor": {"file": "main.py", "line": 1, "end_line": 1},
            "included_spans": [
                {
                    "file": "helper.py",
                    "start_line": 5,
                    "end_line": 5,
                    "retrieval_source": "relation_graph",
                    "context_hash": digest,
                }
            ],
            "included_graph_paths": [],
            "excluded_low_confidence_paths": [],
        }
        for manifest_id, digest in (("C-ONE", "hash-one"), ("C-TWO", "hash-two"))
    ]

    bound, _ = _context(candidates, request, [], manifests)

    evidence = bound[0].issue.contract_evidence[0]
    assert evidence.retrieval_source == "reviewer_context"
    assert evidence.context_manifest_id == ""


def test_unique_manifest_source_and_issue_binding_are_system_owned() -> None:
    request = _request()
    issue = _issue(
        contract_source="model_invented_source",
        contract_file="helper.py",
        contract_line=5,
    )
    issue.context_manifest_id = "C-MODEL-WRONG"
    issue.context_hash = "model-wrong-hash"
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    manifest = {
        "candidate_id": "C-CANONICAL",
        "changed_anchor": {"file": "main.py", "line": 1, "end_line": 1},
        "included_spans": [
            {
                "file": "helper.py",
                "start_line": 5,
                "end_line": 5,
                "retrieval_source": "relation_graph",
                "context_hash": "canonical-hash",
            }
        ],
        "included_graph_paths": [],
        "excluded_low_confidence_paths": [],
    }

    bound, _ = _context(candidates, request, [], [manifest])

    bound_issue = bound[0].issue
    evidence = bound_issue.contract_evidence[0]
    assert bound_issue.context_manifest_id == "C-CANONICAL"
    assert bound_issue.context_hash == ""
    assert evidence.candidate_id == bound[0].candidate_id
    assert evidence.context_manifest_id == "C-CANONICAL"
    assert evidence.context_hash == "canonical-hash"
    assert evidence.retrieval_source == "relation_graph"
