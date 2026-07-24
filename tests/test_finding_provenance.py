"""Manifest-bound evidence and consolidation context-union tests."""

from __future__ import annotations

from src.analyzer.code_graph import ChangedAnchor
from src.analyzer.context_planner import (
    CandidateContextManifest,
    IncludedGraphPath,
    IncludedSpan,
    ManifestGraphEdge,
    extend_manifest,
)
from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RepairIntent,
    SourceAnchor,
    context_hash,
)
from src.analyzer.finding_verifier import build_candidates, validate_verifications
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.root_cause import ConsolidationVerifier, RootCauseConsolidator
from src.analyzer.schemas import (
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
)


def _span(
    manifest: str,
    *,
    file: str = "pkg/service.py",
    line: int = 12,
    content: str = "12: return cache[key]",
) -> IncludedSpan:
    return IncludedSpan(
        span_id=f"span-{manifest}-{line}",
        file=file,
        start_line=line,
        end_line=line,
        symbol_id=f"python|{file}|load|function|10:14",
        role="changed_hunk",
        content=content,
        context_hash=context_hash(content),
        retrieval_source="git_diff",
        forced=True,
        token_cost=max(1, len(content) // 4),
    )


def _manifest(
    manifest_id: str = "C-001",
    *,
    span: IncludedSpan | None = None,
) -> CandidateContextManifest:
    included = span or _span(manifest_id)
    return CandidateContextManifest(
        candidate_id=manifest_id,
        changed_anchor=ChangedAnchor(
            anchor_id="A-001",
            file=included.file,
            line=included.start_line,
            end_line=included.end_line,
            changed_lines=[included.start_line],
            hunk_index=0,
            symbol_id=included.symbol_id,
        ),
        included_spans=[included],
        token_cost=included.token_cost,
        char_cost=len(included.content),
        included_node_count=1,
    )


def _issue(
    finding_id: str = "F-001",
    *,
    manifest: CandidateContextManifest | None = None,
) -> ReviewIssue:
    manifest = manifest or _manifest()
    span = manifest.included_spans[0]
    provenance = EvidenceProvenance(
        candidate_id=manifest.candidate_id,
        context_manifest_id=manifest.candidate_id,
        retrieval_source=span.retrieval_source,
        file=span.file,
        line=span.start_line,
        symbol_id=span.symbol_id,
        context_hash=span.context_hash,
        resolver="git_diff",
        statement="Changed lookup returns a stale cache entry",
    )
    contract = provenance.model_copy(
        update={"statement": "Cache identity must match the requested configuration"}
    )
    return ReviewIssue(
        severity=Severity.WARNING,
        location=f"{span.file}:{span.start_line}",
        evidence="return cache[key]",
        suggestion="Include the requested model in the cache key.",
        confidence=0.95,
        schema_version="2.0",
        finding_id=finding_id,
        primary_anchor=SourceAnchor(
            file=span.file,
            line=span.start_line,
            symbol_id=span.symbol_id,
        ),
        observed_behavior="A stale model is returned",
        causal_mechanism="Cache key omits model identity",
        violated_invariant="Cache identity must match requested model configuration",
        repair_intent=RepairIntent(
            action="Include model in cache identity",
            targets=["Recognizer.cache_key"],
            boundary="cache lifecycle",
        ),
        cause_evidence=[provenance],
        contract_evidence=[contract],
        context_manifest_id=manifest.candidate_id,
    )


def _request() -> ReviewRequest:
    return ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n+return cache[key]\n"
        ),
    )


def _accepted_batch(candidate_id: str) -> FindingVerificationBatch:
    return FindingVerificationBatch(
        results=[
            FindingVerification(
                candidate_id=candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="Evidence supports the hypothesis.",
                verified_evidence=["pkg/service.py:12"],
            )
        ]
    )


def test_verifier_accepts_exact_manifest_span_and_hash() -> None:
    manifest = _manifest()
    candidate = build_candidates(
        ReviewReport(issues=[_issue(manifest=manifest)]), iteration=0
    )[0]
    context = {
        "candidate_id": candidate.candidate_id,
        "context_manifest_id": manifest.candidate_id,
        "included_spans": [
            item.model_dump(mode="json") for item in manifest.included_spans
        ],
    }

    result = validate_verifications(
        [candidate],
        _accepted_batch(candidate.candidate_id),
        _request(),
        candidate_context=[context],
    )

    assert result.results[0].status == "accepted"


def test_verifier_rejects_code_outside_manifest() -> None:
    manifest = _manifest()
    issue = _issue(manifest=manifest)
    outside = issue.cause_evidence[0].model_copy(
        update={
            "file": "pkg/outside.py",
            "line": 8,
            "context_hash": context_hash("8: hidden mechanism"),
        }
    )
    issue.cause_evidence = [outside]
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    context = {
        "candidate_id": candidate.candidate_id,
        "context_manifest_id": manifest.candidate_id,
        "included_spans": [
            item.model_dump(mode="json") for item in manifest.included_spans
        ],
    }

    result = validate_verifications(
        [candidate],
        _accepted_batch(candidate.candidate_id),
        _request(),
        candidate_context=[context],
    )

    assert result.results[0].status == "rejected"
    assert result.results[0].reason_codes == ["deterministic_evidence_invalid"]


def test_context_hash_must_match_actual_included_span() -> None:
    manifest = _manifest()
    issue = _issue(manifest=manifest)
    issue.cause_evidence[0].context_hash = context_hash("different content")
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    context = {
        "candidate_id": candidate.candidate_id,
        "context_manifest_id": manifest.candidate_id,
        "included_spans": [
            item.model_dump(mode="json") for item in manifest.included_spans
        ],
    }

    result = validate_verifications(
        [candidate],
        _accepted_batch(candidate.candidate_id),
        _request(),
        candidate_context=[context],
    )

    assert result.results[0].status == "rejected"


def test_low_confidence_graph_edge_cannot_support_acceptance() -> None:
    manifest = _manifest()
    span = manifest.included_spans[0]
    manifest.included_graph_paths = [
        IncludedGraphPath(
            path_id="path-low",
            node_ids=["source", "target"],
            edges=[
                ManifestGraphEdge(
                    edge_id="edge-low",
                    source="source",
                    target="target",
                    kind="CALLS",
                    path=span.file,
                    line=span.start_line,
                    resolver="ast_bare_name_candidates",
                    confidence=0.4,
                    confidence_tier="AMBIGUOUS",
                    evidence_eligibility="exploratory",
                    reason="multiple candidates",
                )
            ],
            score=0.1,
            semantic_role="execution_flow",
            evidence_eligibility="exploratory",
            explanation="exploration only",
        )
    ]
    issue = _issue(manifest=manifest)
    issue.cause_evidence[0] = issue.cause_evidence[0].model_copy(
        update={
            "edge_kind": "CALLS",
            "edge_confidence": 0.4,
            "resolver": "ast_bare_name_candidates",
            "evidence_eligibility": "exploratory",
        }
    )
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    context = {
        "candidate_id": candidate.candidate_id,
        "context_manifest_id": manifest.candidate_id,
        "included_spans": [
            item.model_dump(mode="json") for item in manifest.included_spans
        ],
        "included_graph_paths": [
            item.model_dump(mode="json") for item in manifest.included_graph_paths
        ],
    }

    result = validate_verifications(
        [candidate],
        _accepted_batch(candidate.candidate_id),
        _request(),
        candidate_context=[context],
    )

    assert result.results[0].status == "rejected"


def test_trimmed_or_discarded_path_cannot_be_accepted_evidence() -> None:
    manifest = _manifest()
    issue = _issue(manifest=manifest)
    discarded_content = "30: caller invokes hidden path"
    issue.cause_evidence[0] = issue.cause_evidence[0].model_copy(
        update={
            "file": "pkg/caller.py",
            "line": 30,
            "context_hash": context_hash(discarded_content),
        }
    )
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    context = {
        "candidate_id": candidate.candidate_id,
        "context_manifest_id": manifest.candidate_id,
        "included_spans": [
            item.model_dump(mode="json") for item in manifest.included_spans
        ],
        "discarded_paths": [
            {
                "path_id": "path-discarded",
                "node_ids": ["caller"],
                "edge_kinds": ["CALLED_BY"],
                "reason": "token_budget",
            }
        ],
    }

    result = validate_verifications(
        [candidate],
        _accepted_batch(candidate.candidate_id),
        _request(),
        candidate_context=[context],
    )

    assert result.results[0].status == "rejected"


def test_consolidation_verifier_rejects_evidence_outside_member_union() -> None:
    first_manifest = _manifest("C-001")
    second_manifest = _manifest(
        "C-002",
        span=_span("C-002", file="pkg/service.py", line=20, content="20: stale cache"),
    )
    first = _issue("F-001", manifest=first_manifest)
    second = _issue("F-002", manifest=second_manifest)
    second.primary_anchor = SourceAnchor(
        file="pkg/service.py",
        line=20,
        symbol_id=second_manifest.included_spans[0].symbol_id,
    )
    second.location = "pkg/service.py:20"
    result = RootCauseConsolidator().consolidate(ReviewReport(issues=[first, second]))
    proposal = result.proposals[0]
    merged = result.report.issues[0].model_copy(deep=True)
    rogue = merged.cause_evidence[0].model_copy(
        update={"context_manifest_id": "C-EXTRA"}
    )
    merged.cause_evidence.append(rogue)

    verdict = ConsolidationVerifier().verify(
        proposal,
        [first, second],
        merged,
    )

    assert set(proposal.allowed_context_manifest_ids) == {"C-001", "C-002"}
    assert verdict.accepted is False
    assert "evidence_outside_member_context_union" in verdict.reasons


def test_extra_retrieval_creates_new_manifest_and_provenance() -> None:
    base = _manifest("C-BASE")
    added = _span(
        "C-EXTRA",
        file="pkg/caller.py",
        line=8,
        content="8: return load()",
    )

    extension = extend_manifest(
        base,
        [added],
        retrieval_source="consolidation_extra_retrieval",
        reason="verify shared caller contract",
    )

    assert extension.candidate_id != base.candidate_id
    assert base.candidate_id in extension.parent_manifest_ids
    assert extension.retrieval_provenance[0]["parent_manifest_id"] == base.candidate_id
    assert (
        added.context_hash in extension.retrieval_provenance[0]["added_context_hashes"]
    )
    assert (
        extension.included_spans[-1].retrieval_source == "consolidation_extra_retrieval"
    )


def test_consolidation_extra_retrieval_requires_explicit_config() -> None:
    base = _manifest("C-BASE")
    extension = extend_manifest(
        base,
        [
            _span(
                "C-EXTRA",
                file="pkg/caller.py",
                line=8,
                content="8: return load()",
            )
        ],
        retrieval_source="consolidation_extra_retrieval",
        reason="verify shared caller contract",
    )
    findings = [
        _issue("F-001", manifest=extension),
        _issue("F-002", manifest=extension),
    ]

    disabled = RootCauseConsolidator(extra_retrieval_enabled=False).consolidate(
        ReviewReport(issues=findings),
        manifests=[extension],
    )
    enabled = RootCauseConsolidator(extra_retrieval_enabled=True).consolidate(
        ReviewReport(issues=findings),
        manifests=[extension],
    )

    assert len(disabled.report.issues) == 2
    assert disabled.verifications[0].accepted is False
    assert "evidence_not_in_manifest" in disabled.verifications[0].reasons
    assert len(enabled.report.issues) == 1
    assert enabled.verifications[0].accepted is True
