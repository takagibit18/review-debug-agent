"""Targeted tests for the Reviewer/runtime diagnostic matrix."""

from eval.reviewer_runtime_smoke import (
    ReviewerRuntimeSmokeReport,
    StageDiagnostic,
    _attribute_failure,
    render_markdown,
)


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


def test_renderer_maps_stages_and_not_reached_na() -> None:
    a = _diagnostic(
        "A-agent-search",
        reviewer_discovered_gold="NO",
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
        pre_verifier="N/A",
        semantic_verifier="N/A",
        deterministic_validation="N/A",
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


def test_failure_attribution_distinguishes_discovery_and_verifier() -> None:
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
        pre_verifier="PASS",
        semantic_verifier="REJECT",
        semantic_verifier_reasons=["claim_not_supported"],
        deterministic_validation="N/A",
        final_finding_survived=False,
        matcher_attempted=False,
        gold_match="NOT_REACHED",
    )

    _attribute_failure(discovery)
    _attribute_failure(verifier)

    assert discovery.failure_stage == "reviewer_discovery"
    assert verifier.failure_stage == "semantic_verifier"
    assert "claim_not_supported" in verifier.failure_evidence


def test_failure_attribution_reaches_matcher_only_after_final_survival() -> None:
    item = _diagnostic(
        "A-agent-search",
        submitted_gold_issue=True,
        pre_verifier="PASS",
        semantic_verifier="ACCEPT",
        deterministic_validation="PASS",
        final_finding_survived=True,
        matcher_attempted=True,
        gold_match="MISS",
    )

    _attribute_failure(item)

    assert item.failure_stage == "matcher"
