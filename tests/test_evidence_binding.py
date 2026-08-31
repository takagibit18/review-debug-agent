"""Trusted provenance binding tests for structured finding evidence."""

from __future__ import annotations

from pathlib import Path

from src.analyzer.evidence_binding import bind_candidate_evidence
from src.analyzer.finding_integrity import FindingIntegrityGuard, build_candidates
from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RepairIntent,
    SourceAnchor,
    context_hash,
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


_MANIFEST_ID = "C-032"
_MANIFEST_CONTENT = "32: contract_value = value"


def _two_hunk_request(repo_path: str = ".") -> ReviewRequest:
    return ReviewRequest(
        repo_path=repo_path,
        diff_mode=True,
        diff_text=(
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -11 +12 @@\n"
            "-old_value = value\n"
            "+changed_value = value + 1\n"
            "@@ -31 +32 @@\n"
            "-old_contract = value\n"
            "+contract_value = value\n"
        ),
    )


def _contract_manifest(
    manifest_id: str = _MANIFEST_ID,
    content: str = _MANIFEST_CONTENT,
) -> dict[str, object]:
    return {
        "candidate_id": manifest_id,
        "changed_anchor": {
            "file": "main.py",
            "line": 12,
            "end_line": 12,
            "changed_lines": [12],
        },
        "included_spans": [
            {
                "span_id": f"span-{manifest_id}-32",
                "file": "main.py",
                "start_line": 32,
                "end_line": 32,
                "symbol_id": "python|main.py|load|function|30:34",
                "role": "contract",
                "content": content,
                "context_hash": context_hash(content),
                "retrieval_source": "relation_graph",
                "forced": True,
                "truncated": False,
                "token_cost": max(1, len(content) // 4),
            }
        ],
        "included_graph_paths": [],
        "excluded_low_confidence_paths": [],
    }


def _two_hunk_issue(
    *,
    manifest_id: str = _MANIFEST_ID,
    manifest_digest: str = "",
) -> ReviewIssue:
    manifest_digest = manifest_digest or context_hash(_MANIFEST_CONTENT)
    issue = _issue(
        contract_source="relation_graph",
        contract_file="main.py",
        contract_line=32,
    )
    cause = issue.cause_evidence[0].model_copy(
        update={
            "retrieval_source": "git_diff",
            "file": "main.py",
            "line": 12,
            "context_manifest_id": "",
            "context_hash": "",
        }
    )
    contract = issue.contract_evidence[0].model_copy(
        update={
            "retrieval_source": "relation_graph",
            "file": "main.py",
            "line": 32,
            "context_manifest_id": manifest_id,
            "context_hash": manifest_digest,
        }
    )
    return issue.model_copy(
        update={
            "location": "main.py:12",
            "primary_anchor": SourceAnchor(file="main.py", line=12, symbol_id="main"),
            "cause_evidence": [cause],
            "contract_evidence": [contract],
            "context_manifest_id": manifest_id,
        }
    )


def _write_two_hunk_repo(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {index}" for index in range(1, 41)) + "\n",
        encoding="utf-8",
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
    *,
    max_chars: int = 12_000,
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
        max_chars=max_chars,
        context_manifests=manifests,
    )


def test_manifest_evidence_keeps_position_32_out_of_diff_hunks(
    tmp_path: Path,
) -> None:
    _write_two_hunk_repo(tmp_path)
    request = _two_hunk_request(str(tmp_path))
    manifest = _contract_manifest()
    candidates = build_candidates(
        ReviewReport(
            issues=[
                _two_hunk_issue(
                    manifest_id=_MANIFEST_ID,
                    manifest_digest=context_hash(_MANIFEST_CONTENT),
                )
            ]
        ),
        iteration=0,
    )

    bound, context = _context(
        candidates,
        request,
        [],
        [manifest],
        max_chars=2_000,
    )

    cause = bound[0].issue.cause_evidence[0]
    contract = bound[0].issue.contract_evidence[0]
    assert cause.file == "main.py"
    assert cause.line == 12
    assert cause.retrieval_source == "git_diff"
    assert contract.file == "main.py"
    assert contract.line == 32
    assert contract.context_manifest_id == _MANIFEST_ID
    assert contract.context_hash == context_hash(_MANIFEST_CONTENT)
    assert [item["new_start"] for item in context[0]["diff_hunks"]] == [12]
    assert [item["start_line"] for item in context[0]["included_spans"]] == [32]
    assert context[0]["file_windows"] == []
    assert context[0]["enclosing_symbols"] == []
    assert context[0]["symbol_contexts"] == []

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[manifest],
        candidate_context=context,
    )

    assert result.passed_count == 1
    assert result.rejected_count == 0


def test_manifest_id_and_hash_mismatch_fails_closed_even_with_matching_diff_hunk(
    tmp_path: Path,
) -> None:
    _write_two_hunk_repo(tmp_path)
    request = _two_hunk_request(str(tmp_path))
    manifest = _contract_manifest()
    candidates = build_candidates(
        ReviewReport(
            issues=[
                _two_hunk_issue(
                    manifest_id="C-WRONG",
                    manifest_digest="wrong-hash",
                )
            ]
        ),
        iteration=0,
    )

    bound, context = _context(
        candidates,
        request,
        [],
        [manifest],
        max_chars=2_000,
    )
    contract = bound[0].issue.contract_evidence[0]
    assert contract.context_manifest_id == "C-WRONG"
    assert contract.context_hash == "wrong-hash"
    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[manifest],
        candidate_context=context,
    )

    assert result.rejected_count == 1
    assert "evidence_not_observed" in {
        failure.code for failure in result.results[0].failures
    }


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
