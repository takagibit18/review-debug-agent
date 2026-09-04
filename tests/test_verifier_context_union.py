"""Multi-manifest verifier-context and budget attribution contracts."""

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
from src.analyzer.verifier_context import (
    build_candidate_verifier_context,
    context_budget_exhausted_for_evidence,
    provenance_in_candidate_context,
)


def _request(repo_path: str) -> ReviewRequest:
    return ReviewRequest(
        repo_path=repo_path,
        diff_mode=True,
        diff_text=(
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -11 +12 @@\n"
            "-old = value\n"
            "+changed = value + 1\n"
        ),
    )


def _manifest(manifest_id: str, line: int) -> dict[str, object]:
    content = f"{line}: contract {manifest_id}"
    return {
        "candidate_id": manifest_id,
        "changed_anchor": {"file": "main.py", "line": 12, "end_line": 12},
        "included_spans": [
            {
                "span_id": f"span-{manifest_id}",
                "file": "main.py",
                "start_line": line,
                "end_line": line,
                "content": content,
                "context_hash": context_hash(content),
                "retrieval_source": "relation_graph",
            }
        ],
        "included_graph_paths": [],
        "excluded_low_confidence_paths": [],
    }


def _issue() -> ReviewIssue:
    return ReviewIssue(
        severity=Severity.WARNING,
        location="main.py:12",
        evidence="The changed value violates the helper contract.",
        suggestion="Keep the increment at one side of the boundary.",
        confidence=0.95,
        schema_version="2.0",
        primary_anchor=SourceAnchor(file="main.py", line=12),
        observed_behavior="The value is incremented twice.",
        causal_mechanism="The caller and helper both increment the value.",
        violated_invariant="The value is incremented exactly once.",
        repair_intent=RepairIntent(action="Remove one increment"),
        cause_evidence=[
            EvidenceProvenance(
                retrieval_source="git_diff",
                file="main.py",
                line=12,
                statement="The changed caller increments before invoking the helper.",
            )
        ],
        contract_evidence=[
            EvidenceProvenance(
                retrieval_source="relation_graph",
                context_manifest_id="C-ONE",
                context_hash=context_hash("32: contract C-ONE"),
                file="main.py",
                line=32,
                statement="The helper contract already increments the value.",
            )
        ],
        trigger_evidence=[
            EvidenceProvenance(
                retrieval_source="relation_graph",
                context_manifest_id="C-TWO",
                context_hash=context_hash("36: contract C-TWO"),
                file="main.py",
                line=36,
                statement="The same value reaches the second contract boundary.",
            )
        ],
    )


def test_context_union_retains_each_declared_manifest_provenance(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    candidates = build_candidates(ReviewReport(issues=[_issue()]), iteration=0)
    manifests = [_manifest("C-ONE", 32), _manifest("C-TWO", 36)]

    bound = bind_candidate_evidence(candidates, request, [], context_manifests=manifests)
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=manifests, max_chars=12_000
    )

    assert set(context[0]["context_manifest_ids"]) == {"C-ONE", "C-TWO"}
    assert {
        item["context_manifest_id"] for item in context[0]["manifest_envelopes"]
    } == {"C-ONE", "C-TWO"}
    assert {
        item["context_manifest_id"] for item in context[0]["included_spans"]
    } == {"C-ONE", "C-TWO"}

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=manifests,
        candidate_context=context,
    )
    assert result.passed_count == 1


def test_budget_failure_is_not_reported_as_unobserved(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger_evidence = []
    manifest = _manifest("C-ONE", 32)
    manifest["included_spans"][0]["content"] = "x" * 4_000
    manifest["included_spans"][0]["context_hash"] = context_hash("x" * 4_000)
    issue.contract_evidence[0].context_hash = context_hash("x" * 4_000)
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    bound = bind_candidate_evidence(
        candidates, request, [], context_manifests=[manifest]
    )
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=[manifest], max_chars=800
    )
    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[manifest],
        candidate_context=context,
    )

    failure_codes = {failure.code for failure in result.results[0].failures}
    assert "verifier_context_budget_exhausted" in failure_codes
    assert "evidence_not_observed" not in failure_codes


def test_same_location_in_different_manifests_has_exact_budget_attribution(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger = "The value reaches both manifest-backed boundaries."
    first_content = "32: " + "A" * 900
    second_content = "32: " + "B" * 900
    issue.contract_evidence = [
        EvidenceProvenance(
            retrieval_source="relation_graph",
            context_manifest_id="C-ONE",
            context_hash=context_hash(first_content),
            file="main.py",
            line=32,
            statement="The first manifest owns this contract evidence.",
        )
    ]
    issue.trigger_evidence = [
        EvidenceProvenance(
            retrieval_source="relation_graph",
            context_manifest_id="C-TWO",
            context_hash=context_hash(second_content),
            file="main.py",
            line=32,
            statement="The second manifest owns distinct trigger evidence.",
        )
    ]
    manifests = [_manifest("C-ONE", 32), _manifest("C-TWO", 32)]
    for manifest, content in zip(
        manifests, (first_content, second_content), strict=True
    ):
        manifest["included_spans"][0]["content"] = content
        manifest["included_spans"][0]["context_hash"] = context_hash(content)

    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    bound = bind_candidate_evidence(
        candidates, request, [], context_manifests=manifests
    )
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=manifests, max_chars=3_200
    )
    candidate_context = context[0]

    assert [
        span["context_manifest_id"]
        for span in candidate_context["included_spans"]
    ] == ["C-ONE"]
    assert not context_budget_exhausted_for_evidence(
        candidate_context,
        bound[0].issue.contract_evidence[0],
        role="contract",
    )
    assert context_budget_exhausted_for_evidence(
        candidate_context,
        bound[0].issue.trigger_evidence[0],
        role="trigger",
    )
    other_candidate_evidence = bound[0].issue.trigger_evidence[0].model_copy(
        update={"candidate_id": "other-candidate"}
    )
    assert not context_budget_exhausted_for_evidence(
        candidate_context,
        other_candidate_evidence,
        role="trigger",
    )
    assert {
        (record["role"], record["context_manifest_id"])
        for record in candidate_context["budget_exhausted_locations"]
        if record["request_kind"] == "evidence"
    } == {("trigger", "C-TWO")}

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=manifests,
        candidate_context=context,
    )
    trigger_codes = {
        failure.code
        for failure in result.results[0].failures
        if failure.field == "trigger_evidence[0]"
    }
    assert trigger_codes == {"verifier_context_budget_exhausted"}


def test_graph_path_without_source_span_is_not_budget_exhaustion(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger_evidence = []
    issue.contract_evidence = [
        EvidenceProvenance(
            retrieval_source="relation_graph",
            context_manifest_id="C-GRAPH",
            context_hash=context_hash("32: source span that was never retained"),
            file="main.py",
            line=32,
            edge_kind="calls",
            edge_confidence=0.9,
            resolver="ast",
            statement="The graph edge reaches the claimed contract.",
        )
    ]
    manifest = _manifest("C-GRAPH", 32)
    manifest["included_spans"] = []
    manifest["included_graph_paths"] = [
        {
            "path_id": "graph-only",
            "evidence_eligibility": "strong",
            "edges": [
                {
                    "file": "main.py",
                    "start_line": 32,
                    "end_line": 32,
                    "kind": "calls",
                    "confidence": 0.9,
                    "resolver": "ast",
                    "evidence_eligibility": "strong",
                }
            ],
        }
    ]

    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    bound = bind_candidate_evidence(
        candidates, request, [], context_manifests=[manifest]
    )
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=[manifest], max_chars=12_000
    )
    candidate_context = context[0]
    evidence = bound[0].issue.contract_evidence[0]

    assert candidate_context["included_graph_paths"]
    assert candidate_context["included_spans"] == []
    assert not candidate_context["verifier_context_budget_exhausted"]
    assert not context_budget_exhausted_for_evidence(
        candidate_context, evidence, role="contract"
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[manifest],
        candidate_context=context,
    )
    contract_codes = {
        failure.code
        for failure in result.results[0].failures
        if failure.field == "contract_evidence[0]"
    }
    assert contract_codes == {"evidence_not_observed"}


def test_clipped_source_span_cannot_authorize_missing_evidence_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 121)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger_evidence = []
    content = "\n".join(
        f"{line}: {'x' * 80}" for line in range(20, 101)
    )
    issue.contract_evidence = [
        EvidenceProvenance(
            retrieval_source="relation_graph",
            context_manifest_id="C-CLIPPED",
            context_hash=context_hash(content),
            file="main.py",
            line=80,
            statement="Line 80 contains the contract required by the finding.",
        )
    ]
    manifest = _manifest("C-CLIPPED", 80)
    manifest["included_spans"][0].update(
        {
            "start_line": 20,
            "end_line": 100,
            "content": content,
            "context_hash": context_hash(content),
        }
    )

    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    bound = bind_candidate_evidence(
        candidates, request, [], context_manifests=[manifest]
    )
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=[manifest], max_chars=12_000
    )
    candidate_context = context[0]
    retained_span = candidate_context["included_spans"][0]
    evidence = bound[0].issue.contract_evidence[0]

    assert retained_span["_verifier_text_clipped"] is True
    assert not any(
        line.startswith("80:") for line in retained_span["content"].splitlines()
    )
    assert not provenance_in_candidate_context(candidate_context, evidence)
    assert context_budget_exhausted_for_evidence(
        candidate_context, evidence, role="contract"
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[manifest],
        candidate_context=context,
    )
    contract_codes = {
        failure.code
        for failure in result.results[0].failures
        if failure.field == "contract_evidence[0]"
    }
    assert contract_codes == {"verifier_context_budget_exhausted"}


def test_clipped_nested_tool_record_cannot_authorize_missing_evidence_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 121)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger_evidence = []
    issue.contract_evidence = [
        EvidenceProvenance(
            retrieval_source="find_symbol_context",
            file="helper.py",
            line=80,
            statement="Line 80 contains the helper contract.",
        )
    ]
    content = "\n".join(
        f"{line}: {'x' * 80}" for line in range(20, 101)
    )
    tool_evidence = [
        {
            "tool_name": "find_symbol_context",
            "arguments": {"symbol": "helper", "path": "helper.py"},
            "data": {
                "path": "helper.py",
                "symbol": "helper",
                "definitions": [
                    {
                        "path": "helper.py",
                        "start_line": 20,
                        "end_line": 100,
                        "content": content,
                    }
                ],
                "references": [],
                "enclosing_symbols": [],
            },
        }
    ]

    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    bound = bind_candidate_evidence(candidates, request, tool_evidence)
    context = build_candidate_verifier_context(
        bound, request, tool_evidence, max_chars=12_000
    )
    candidate_context = context[0]
    retained_record = candidate_context["symbol_contexts"][0]["definitions"][0]
    evidence = bound[0].issue.contract_evidence[0]

    assert retained_record["_verifier_text_clipped"] is True
    assert not any(
        line.startswith("80:") for line in retained_record["content"].splitlines()
    )
    assert not provenance_in_candidate_context(candidate_context, evidence)
    assert context_budget_exhausted_for_evidence(
        candidate_context, evidence, role="contract"
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        tool_evidence=tool_evidence,
        candidate_context=context,
    )
    contract_codes = {
        failure.code
        for failure in result.results[0].failures
        if failure.field == "contract_evidence[0]"
    }
    assert contract_codes == {"verifier_context_budget_exhausted"}


def test_unavailable_evidence_is_not_labeled_as_budget_exhaustion(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 41)) + "\n",
        encoding="utf-8",
    )
    request = _request(str(tmp_path))
    issue = _issue()
    issue.trigger_evidence = []
    issue.contract_evidence = [
        EvidenceProvenance(
            retrieval_source="relation_graph",
            context_manifest_id="C-MISSING",
            context_hash=context_hash("32: unavailable"),
            file="main.py",
            line=32,
            statement="This evidence was not present in the source data.",
        )
    ]

    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    bound = bind_candidate_evidence(candidates, request, [], context_manifests=[])
    context = build_candidate_verifier_context(
        bound, request, [], context_manifests=[], max_chars=12_000
    )
    candidate_context = context[0]
    evidence = bound[0].issue.contract_evidence[0]

    assert not context_budget_exhausted_for_evidence(
        candidate_context, evidence, role="contract"
    )
    retention = next(
        record
        for record in candidate_context["evidence_retention"]
        if record["role"] == "contract"
    )
    assert retention["available"] is False
    assert retention["omission_reason"] == "not_available"

    result = FindingIntegrityGuard(tmp_path).validate(
        bound,
        request,
        context_manifests=[],
        candidate_context=context,
    )
    contract_codes = {
        failure.code
        for failure in result.results[0].failures
        if failure.field == "contract_evidence[0]"
    }
    assert contract_codes == {"evidence_not_observed"}
