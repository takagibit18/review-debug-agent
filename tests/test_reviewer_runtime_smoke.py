"""Targeted tests for the Reviewer/runtime diagnostic matrix."""

from eval.core_eval import CoreFixtureSpec, GoldFinding, GoldLocation
from eval.reviewer_runtime_smoke import (
    ReviewerRuntimeSmokeReport,
    StageDiagnostic,
    _attribute_failure,
    _semantic_grade,
    render_markdown,
)
from src.analyzer.output_formatter import Severity


def _diagnostic(variant_id: str, **overrides: object) -> StageDiagnostic:
    payload: dict[str, object] = {
        "variant_id": variant_id,
        "workspace_valid": "PASS",
        "fixture_validation_passed": True,
        "runtime_valid_completion": "PASS",
        "valid_completion": True,
        "schema_valid": True,
        "variant_contract_valid": True,
        "gold_file_reached": True,
        "gold_symbol_reached": True,
        "reviewer_discovered_gold": "YES",
        "submit_review": "NORMAL",
        "length_recovery": "NOT_REQUIRED",
        "final_finding_survived": True,
        "matcher_attempted": True,
        "gold_match": "HIT",
        "failure_stage": "none",
        "failure_evidence": "no pipeline failure observed",
        "final_diagnosis": "complete chain; gold matched",
    }
    payload.update(overrides)
    return StageDiagnostic.model_validate(payload)


def _report(a: StageDiagnostic, b: StageDiagnostic) -> ReviewerRuntimeSmokeReport:
    return ReviewerRuntimeSmokeReport(
        fixture_id="golden_pytest-dev_pytest_pr9350",
        fixture_path="eval/fixtures/golden_pytest-dev_pytest_pr9350.json",
        gold_description="wrapper equality bug",
        runtime_contract={
            "model": "deepseek-v4-pro",
            "model_max_tokens": 4096,
        },
        diagnostics=[a, b],
        ready_for_formal_graph_ab="GO",
        conclusion_reason="valid",
        next_step="formal Graph A/B",
    )


def _pytest9350_spec() -> CoreFixtureSpec:
    return CoreFixtureSpec(
        fixture_id="golden_pytest-dev_pytest_pr9350",
        path="eval/fixtures/golden_pytest-dev_pytest_pr9350.json",
        role="candidate",
        gold_findings=[
            GoldFinding(
                id="safe-hash-wrapper-compares-wrapper",
                category="logic",
                severity=Severity.WARNING,
                file="src/_pytest/fixtures.py",
                location=GoldLocation(start_line=244, end_line=250),
                description=(
                    "SafeHashWrapper.__eq__ compares self.obj with the other wrapper "
                    "object instead of other.obj, so equal wrapped parameter values "
                    "can compare unequal and fixture grouping can break."
                ),
                root_cause=(
                    "Equality fails to unwrap the peer SafeHashWrapper before comparing "
                    "the wrapped parameter values."
                ),
            )
        ],
    )


def test_discovery_grades_explicit_wrapper_semantics_as_yes() -> None:
    claim = (
        "SafeHashWrapper.__eq__ compares self.obj against the other wrapper object "
        "instead of other.obj, so wrappers around equal values can compare unequal."
    )

    assert _semantic_grade(claim, _pytest9350_spec()) == "YES"


def test_discovery_keeps_vague_symbol_suspicion_partial() -> None:
    assert (
        _semantic_grade(
            "SafeHashWrapper equality may be problematic; inspect it.",
            _pytest9350_spec(),
        )
        == "PARTIAL"
    )


def test_renderer_maps_stages_and_not_reached_na() -> None:
    a = _diagnostic(
        "A-agent-search",
        reviewer_discovered_gold="NO",
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
        finding_policy="N/A",
        integrity_validation="N/A",
        graph_manifest_contains_gold=None,
    )
    b = _diagnostic(
        "B1-graph-hybrid-cold",
        submit_review="RECOVERY",
        length_recovery="SUCCESS",
        graph_manifest_contains_gold=True,
    )

    markdown = render_markdown(_report(a, b))

    assert "| Gold match | NOT_REACHED | HIT |" in markdown
    assert "| Graph manifest valid | N/A | YES |" in markdown
    assert "| Length recovery | NOT_REQUIRED | SUCCESS |" in markdown
    assert "| submit_review | NORMAL | RECOVERY |" in markdown


def test_failure_attribution_distinguishes_discovery_and_integrity() -> None:
    discovery = _diagnostic(
        "A-agent-search",
        reviewer_discovered_gold="NO",
        submitted_gold_issue=False,
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
    )
    verifier = _diagnostic(
        "B1-graph-hybrid-cold",
        submitted_gold_issue=True,
        finding_policy="PASS",
        integrity_validation="REJECT",
        integrity_reasons=["evidence_not_observed"],
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
    )

    _attribute_failure(discovery)
    _attribute_failure(verifier)

    assert discovery.failure_stage == "reviewer_discovery"
    assert verifier.failure_stage == "integrity_validation"
    assert "evidence_not_observed" in verifier.failure_evidence


def test_provider_failure_precedes_structured_submit() -> None:
    item = _diagnostic(
        "A-agent-search",
        runtime_valid_completion="FAIL",
        valid_completion=False,
        model_provider_call_errors=["request timeout"],
        submit_review="NO",
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
    )

    _attribute_failure(item)

    assert item.failure_stage == "provider_request"


def test_failure_attribution_reaches_matcher_only_after_final_survival() -> None:
    item = _diagnostic(
        "A-agent-search",
        submitted_gold_issue=True,
        finding_policy="PASS",
        integrity_validation="PASS",
        final_finding_survived=True,
        matcher_attempted=True,
        gold_match="MISS",
    )

    _attribute_failure(item)

    assert item.failure_stage == "matcher"
