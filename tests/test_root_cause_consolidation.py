"""Root-cause merger regression cases for v0.2.3."""

from __future__ import annotations

import pytest

import src.analyzer.root_cause as root_cause_module
from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RelatedLocation,
    RepairIntent,
    SourceAnchor,
    context_hash,
)
from src.analyzer.finding_verifier import apply_verifications, build_candidates
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.root_cause import (
    ConsolidationVerification,
    ConsolidationVerifier,
    CausalityRelation,
    RootCauseConsolidator,
)
from src.analyzer.schemas import FindingVerification, FindingVerificationBatch


def _evidence(
    file: str,
    line: int,
    statement: str,
    *,
    manifest: str = "C-001",
) -> EvidenceProvenance:
    content = f"{line}: {statement}"
    return EvidenceProvenance(
        candidate_id=manifest,
        context_manifest_id=manifest,
        retrieval_source="relation_graph",
        file=file,
        line=line,
        symbol_id=f"python|{file}|symbol|method|{line}:{line}",
        context_hash=context_hash(content),
        resolver="python_ast",
        statement=statement,
    )


def _finding(
    finding_id: str,
    *,
    file: str,
    line: int,
    mechanism: str,
    invariant: str,
    action: str,
    targets: list[str],
    boundary: str,
    trigger: str = "",
    impact: str = "",
    manifest: str = "C-001",
) -> ReviewIssue:
    cause = _evidence(file, line, mechanism, manifest=manifest)
    contract = _evidence(file, line + 1, invariant, manifest=manifest)
    return ReviewIssue(
        severity=Severity.WARNING,
        location=f"{file}:{line}",
        evidence=mechanism,
        suggestion=action,
        confidence=0.95,
        candidate_id=f"candidate-{finding_id}",
        schema_version="2.0",
        finding_id=finding_id,
        primary_anchor=SourceAnchor(
            file=file,
            line=line,
            symbol_id=f"python|{file}|Scope.{finding_id}|method|{line}:{line + 2}",
        ),
        observed_behavior=f"Observed behavior for {finding_id}",
        causal_mechanism=mechanism,
        violated_invariant=invariant,
        repair_intent=RepairIntent(
            action=action,
            targets=targets,
            boundary=boundary,
        ),
        trigger=trigger,
        impact=impact,
        cause_evidence=[cause],
        contract_evidence=[contract],
        trigger_evidence=[_evidence(file, line, trigger, manifest=manifest)]
        if trigger
        else [],
        impact_evidence=[_evidence(file, line, impact, manifest=manifest)]
        if impact
        else [],
        context_manifest_id=manifest,
    )


def test_safe_hash_wrapper_contract_is_one_multi_location_root_cause() -> None:
    common = dict(
        mechanism="__eq__ and __hash__ use incompatible wrapped-value semantics",
        invariant="Objects equal by equality must have the same hash",
        action="Align equality and hash implementations",
        targets=["SafeHashWrapper.__eq__", "SafeHashWrapper.__hash__"],
        boundary="equality hash pair",
    )
    equality = _finding(
        "F-EQ",
        file="safe_hash.py",
        line=10,
        trigger="Two wrappers compare equal",
        **common,
    )
    hashing = _finding(
        "F-HASH",
        file="safe_hash.py",
        line=20,
        impact="Equal wrappers occupy different dict buckets",
        **common,
    )

    result = RootCauseConsolidator().consolidate(
        ReviewReport(issues=[equality, hashing])
    )

    assert result.metrics.accepted_cluster_count == 1
    assert result.metrics.final_root_cause_count == 1
    merged = result.report.issues[0]
    assert set(merged.member_findings) == {"F-EQ", "F-HASH"}
    assert {item.location for item in merged.related_locations} | {
        merged.primary_anchor.location
    } == {"safe_hash.py:10", "safe_hash.py:20"}
    assert len(merged.cause_evidence) == 2
    assert merged.root_cause_id.startswith("RC-")


def test_vosk_cache_symptoms_merge_but_independent_issues_stay_separate() -> None:
    cache_repair = dict(
        invariant="Cached model identity must match requested model and language configuration",
        action="Include model and language in cache identity",
        targets=["Recognizer._model_cache_key"],
        boundary="cache lifecycle",
    )
    findings = [
        _finding(
            "F-MODEL",
            file="vosk.py",
            line=30,
            mechanism="Cache key omits model and language identity",
            impact="Model switch keeps the stale model",
            **cache_repair,
        ),
        _finding(
            "F-LANGUAGE",
            file="vosk.py",
            line=40,
            mechanism="Stale cache reuse ignores requested language configuration",
            trigger="Language changes while a model remains cached",
            **cache_repair,
        ),
        _finding(
            "F-DOWNLOAD-STALE",
            file="vosk.py",
            line=50,
            mechanism="Downloaded model is bypassed by cache identity reuse",
            impact="Download completes but the old model is returned",
            **cache_repair,
        ),
        _finding(
            "F-DEFAULT",
            file="vosk.py",
            line=60,
            mechanism="Default language value changes without compatibility handling",
            invariant="The public default language must remain backward compatible",
            action="Restore or migrate the default language value",
            targets=["Recognizer.default_language"],
            boundary="public constructor default",
        ),
        _finding(
            "F-SYNC",
            file="vosk.py",
            line=70,
            mechanism="Synchronous network download blocks the recognition path",
            invariant="Recognition entry points must not perform blocking network I/O",
            action="Move model download to an asynchronous preparation step",
            targets=["Recognizer.download_model"],
            boundary="download execution path",
        ),
    ]

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=findings))

    assert result.metrics.final_root_cause_count == 3
    cache = next(
        issue
        for issue in result.report.issues
        if set(issue.member_findings)
        == {
            "F-MODEL",
            "F-LANGUAGE",
            "F-DOWNLOAD-STALE",
        }
    )
    assert cache.counterfactual_result == "yes"
    assert any(issue.member_findings == ["F-DEFAULT"] for issue in result.report.issues)
    assert any(issue.member_findings == ["F-SYNC"] for issue in result.report.issues)


def test_same_module_and_similar_wording_do_not_override_different_repairs() -> None:
    first = _finding(
        "F-A",
        file="service.py",
        line=10,
        mechanism="Cache identity drops tenant state",
        invariant="Cache identity must include tenant configuration",
        action="Include tenant in cache identity",
        targets=["Service.cache_key"],
        boundary="cache lifecycle",
    )
    second = _finding(
        "F-B",
        file="service.py",
        line=20,
        mechanism="Cache identity drops tenant state",
        invariant="Cache identity must include tenant configuration",
        action="Invalidate results when policy changes",
        targets=["Service.policy_cache"],
        boundary="policy invalidation lifecycle",
    )

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=[first, second]))

    assert result.metrics.accepted_cluster_count == 0
    assert len(result.report.issues) == 2


def test_different_mechanism_wording_merges_when_hard_criteria_match() -> None:
    common = dict(
        invariant="Cache identity must match requested model configuration",
        action="Include model and language in cache identity",
        targets=["Recognizer._model_cache_key"],
        boundary="cache lifecycle",
    )
    first = _finding(
        "F-A",
        file="recognizer.py",
        line=10,
        mechanism="Cache key omits model identity",
        **common,
    )
    second = _finding(
        "F-B",
        file="recognizer.py",
        line=20,
        mechanism="Stale cache reuse ignores language configuration",
        **common,
    )

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=[first, second]))

    assert len(result.report.issues) == 1


def test_uncertain_incomplete_repair_stays_separate() -> None:
    first = _finding(
        "F-A",
        file="cache.py",
        line=10,
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="cache lifecycle",
    )
    second = _finding(
        "F-B",
        file="cache.py",
        line=20,
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="",
    )

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=[first, second]))

    assert len(result.report.issues) == 2
    assert result.proposals == []


def test_complete_link_prevents_transitive_three_node_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = [
        _finding(
            name,
            file="chain.py",
            line=index * 10,
            mechanism="Cache key omits model identity",
            invariant="Cache identity must match requested model configuration",
            action="Include model in cache identity",
            targets=["cache_key"],
            boundary="cache lifecycle",
        )
        for index, name in enumerate(("A", "B", "C"), start=1)
    ]

    def compatibility(left: ReviewIssue, right: ReviewIssue):  # type: ignore[no-untyped-def]
        pair = frozenset({left.finding_id, right.finding_id})
        if pair in {frozenset({"A", "B"}), frozenset({"B", "C"})}:
            return "yes", {"mechanism", "invariant", "repair"}
        return "no", {"mechanism", "invariant"}

    monkeypatch.setattr(root_cause_module, "_merge_compatible", compatibility)

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=findings))

    assert all(len(proposal.member_findings) == 2 for proposal in result.proposals)
    assert not any(len(issue.member_findings) == 3 for issue in result.report.issues)


def test_merge_verifier_rejection_falls_back_to_original_findings() -> None:
    class RejectingVerifier(ConsolidationVerifier):
        def verify(self, proposal, members, merged, **kwargs):  # type: ignore[no-untyped-def]
            return ConsolidationVerification(
                root_cause_id=proposal.root_cause_id,
                accepted=False,
                reasons=["independent_problem_absorbed"],
            )

    common = dict(
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="cache lifecycle",
    )
    first = _finding("F-A", file="cache.py", line=10, **common)
    second = _finding("F-B", file="cache.py", line=20, **common)

    result = RootCauseConsolidator(verifier=RejectingVerifier()).consolidate(
        ReviewReport(issues=[first, second])
    )

    assert len(result.report.issues) == 2
    assert all(
        "independent_problem_absorbed" in issue.merge_rejection_reasons
        for issue in result.report.issues
    )


def test_unsupported_finding_is_removed_before_merger() -> None:
    supported = _finding(
        "F-SUPPORTED",
        file="service.py",
        line=10,
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="cache lifecycle",
    )
    unsupported = _finding(
        "F-UNSUPPORTED",
        file="service.py",
        line=20,
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="cache lifecycle",
    )
    report = ReviewReport(issues=[supported, unsupported])
    candidates = build_candidates(report, iteration=0)
    batch = FindingVerificationBatch(
        results=[
            FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted"
                if candidate.issue.finding_id == "F-SUPPORTED"
                else "rejected",
                reason_codes=["verified"]
                if candidate.issue.finding_id == "F-SUPPORTED"
                else ["claim_not_supported"],
                rationale="verdict",
                verified_evidence=[candidate.issue.location]
                if candidate.issue.finding_id == "F-SUPPORTED"
                else [],
            )
            for candidate in candidates
        ]
    )

    verified = apply_verifications(report, batch, mode="enforce")
    result = RootCauseConsolidator().consolidate(verified)

    assert result.metrics.input_verified_findings == 1
    assert result.report.issues[0].finding_id == "F-SUPPORTED"


def test_finding_causality_graph_records_hard_criteria_and_absorbed_role() -> None:
    common = dict(
        mechanism="Cache key omits model identity",
        invariant="Cache identity must match requested model configuration",
        action="Include model in cache identity",
        targets=["cache_key"],
        boundary="cache lifecycle",
    )
    cause = _finding("F-CAUSE", file="cache.py", line=10, **common)
    impact = _finding(
        "F-IMPACT",
        file="cache.py",
        line=20,
        impact="Old model remains visible",
        **common,
    )

    result = RootCauseConsolidator().consolidate(ReviewReport(issues=[cause, impact]))
    kinds = {edge.kind for edge in result.causality_graph.edges}

    assert CausalityRelation.SAME_CAUSAL_MECHANISM in kinds
    assert CausalityRelation.VIOLATES_SAME_INVARIANT in kinds
    assert CausalityRelation.SAME_REPAIR_UNIT in kinds
    assert CausalityRelation.IMPACT_OF in kinds


def test_consolidation_uses_changed_cause_evidence_not_display_anchor() -> None:
    common = dict(
        mechanism="Changed dispatch sends a value rejected by the consumer",
        invariant="The dispatched value must satisfy the consumer contract",
        action="Restore the dispatched value contract",
        targets=["dispatch", "consume"],
        boundary="caller/consumer boundary",
    )
    first = _finding("F-A", file="service.py", line=10, **common)
    second = _finding("F-B", file="service.py", line=20, **common)
    consolidated = RootCauseConsolidator().consolidate(
        ReviewReport(issues=[first, second])
    )
    proposal = consolidated.proposals[0]
    merged = consolidated.report.issues[0].model_copy(deep=True)
    original_primary = merged.primary_anchor
    assert original_primary is not None
    merged.related_locations.append(
        RelatedLocation(
            file=original_primary.file,
            line=original_primary.line,
            end_line=original_primary.end_line,
            symbol_id=original_primary.symbol_id,
        )
    )
    merged.primary_anchor = SourceAnchor(file="consumer.py", line=8)
    merged.location = "consumer.py:8"
    changed_cause_diff = (
        "diff --git a/service.py b/service.py\n"
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -9,0 +10,1 @@\n"
        "+dispatch_changed_value()\n"
    )

    accepted = ConsolidationVerifier().verify(
        proposal,
        [first, second],
        merged,
        diff_text=changed_cause_diff,
    )
    rejected = ConsolidationVerifier().verify(
        proposal,
        [first, second],
        merged,
        diff_text=changed_cause_diff.replace("+10,1", "+30,1"),
    )

    assert accepted.accepted is True
    assert "primary_anchor_not_changed_line" not in accepted.reasons
    assert rejected.accepted is False
    assert "pr_causal_anchor_missing_changed_line" in rejected.reasons
