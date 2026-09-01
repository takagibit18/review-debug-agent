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
from src.analyzer.verifier_context import build_candidate_verifier_context


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
    candidates = build_candidates(ReviewReport(issues=[issue]), iteration=0)
    manifest = _manifest("C-ONE", 32)
    manifest["included_spans"][0]["content"] = "x" * 4_000
    manifest["included_spans"][0]["context_hash"] = context_hash("x" * 4_000)

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
