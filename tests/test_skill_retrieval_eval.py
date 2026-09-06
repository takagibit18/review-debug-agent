"""Offline tests for Review Skill retrieval eval readiness."""

from __future__ import annotations

from pathlib import Path

from eval.compare import compare_reports
from eval.runner import _effective_skill_contract, _skill_result_fields, _variant_result_fields
from eval.schemas import EvalReport, EvalResult, EvalVariant, MetricSummary
from eval.skill_retrieval import evaluate_retrieval


ROOT = Path(__file__).resolve().parents[1]


def test_old_eval_variant_defaults_to_sequential() -> None:
    variant = EvalVariant(
        id="legacy",
        context_mode="agent_search",
        graph_cache_mode="disabled",
    )
    assert variant.skill_retrieval_mode == "sequential"
    assert variant.skill_bank_path == ""


def test_eval_variant_resolves_the_same_skill_loader_contract_as_production(
    monkeypatch,
) -> None:
    monkeypatch.setenv("REVIEW_SKILL_TOP_K", "3")
    monkeypatch.setenv("REVIEW_SKILL_CHAR_BUDGET", "2500")
    monkeypatch.setenv("REVIEW_SKILL_LEGACY_FALLBACK_LIMIT", "0")
    variant = EvalVariant(
        id="deterministic-contract",
        context_mode="agent_search",
        graph_cache_mode="disabled",
        skill_retrieval_mode="deterministic",
    )

    contract = _effective_skill_contract(variant)

    assert contract == {
        "skill_top_k": 3,
        "skill_char_budget": 2500,
        "skill_legacy_fallback_limit": 0,
    }
    result_fields = _variant_result_fields(variant, contract)
    assert {name: result_fields[name] for name in contract} == contract


def test_eval_variant_explicit_skill_contract_overrides_settings(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_SKILL_TOP_K", "3")
    monkeypatch.setenv("REVIEW_SKILL_CHAR_BUDGET", "2500")
    monkeypatch.setenv("REVIEW_SKILL_LEGACY_FALLBACK_LIMIT", "0")
    variant = EvalVariant(
        id="explicit-contract",
        context_mode="agent_search",
        graph_cache_mode="disabled",
        skill_top_k=7,
        skill_char_budget=1800,
        skill_legacy_fallback_limit=2,
    )

    assert _effective_skill_contract(variant) == {
        "skill_top_k": 7,
        "skill_char_budget": 1800,
        "skill_legacy_fallback_limit": 2,
    }


def test_retrieval_only_report_meets_offline_gate_and_is_deterministic() -> None:
    kwargs = {
        "fixtures_dir": ROOT / "eval" / "fixtures",
        "skill_bank": ROOT / "eval" / "skill_banks" / "retrieval-v1",
    }
    first = evaluate_retrieval(**kwargs)
    second = evaluate_retrieval(**kwargs)
    assert first == second
    assert first["annotated_fixture_count"] >= 5
    assert first["metrics"] == {
        "recall_at_k": 1.0,
        "precision_at_k": 1.0,
        "irrelevant_rate": 0.0,
        "budget_loss_rate": 0.0,
        "candidate_or_deprecated_selection_count": 0,
        "hard_budget_violation_count": 0,
    }


def test_skill_metrics_exclude_unannotated_results_from_denominator() -> None:
    annotated = EvalResult(
        fixture_id="annotated",
        fixture_type="review",
        schema_valid=True,
        expected_skill_ids=["skill-a"],
        retrieved_skill_ids=["skill-a", "skill-extra"],
        skill_recall_at_k=1.0,
        skill_precision_at_k=0.5,
        skill_irrelevant_rate=0.5,
    )
    unannotated = EvalResult(
        fixture_id="legacy",
        fixture_type="review",
        schema_valid=True,
        expected_skill_ids=None,
        retrieved_skill_ids=["skill-extra"],
    )
    metrics = MetricSummary.from_results([annotated, unannotated])
    assert metrics.skill_recall_at_k == 1.0
    assert metrics.skill_precision_at_k == 0.5
    assert metrics.skill_irrelevant_rate == 0.5


def test_compare_reports_exposes_optional_skill_deltas_without_new_gate() -> None:
    baseline = EvalReport(
        metrics=MetricSummary(
            skill_recall_at_k=0.8,
            skill_precision_at_k=0.5,
            skill_irrelevant_rate=0.5,
        )
    )
    candidate = EvalReport(
        metrics=MetricSummary(
            skill_recall_at_k=1.0,
            skill_precision_at_k=1.0,
            skill_irrelevant_rate=0.0,
        )
    )
    comparison = compare_reports(baseline, candidate)
    assert comparison.skill_recall_at_k_delta == 0.2
    assert comparison.skill_precision_at_k_delta == 0.5
    assert comparison.skill_irrelevant_rate_delta == -0.5
    assert comparison.passed is True


def test_compare_reports_rejects_mismatched_fixed_skill_banks() -> None:
    baseline = EvalReport(skill_bank_digest="a")
    candidate = EvalReport(skill_bank_digest="b")
    comparison = compare_reports(baseline, candidate)
    assert comparison.skill_bank_digest_match is False
    assert comparison.failures == ["skill_bank_digest_mismatch"]


def test_compare_reports_rejects_a_missing_fixed_skill_bank_digest() -> None:
    comparison = compare_reports(
        EvalReport(skill_bank_digest=""),
        EvalReport(skill_bank_digest="candidate-bank"),
    )
    assert comparison.skill_bank_digest_match is False
    assert comparison.failures == ["skill_bank_digest_mismatch"]


def test_budget_loss_includes_skills_after_sequential_budget_break() -> None:
    fields = _skill_result_fields(
        ["skill-first", "skill-second"],
        [],
        (
            ("skill-first", "budget"),
            ("skill-second", "after_budget_break"),
        ),
    )
    assert fields["skill_budget_loss_count"] == 2
