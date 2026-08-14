"""Tests for model prompt contracts."""

from src.analyzer.prompts import (
    AGENT_SEARCH_POLICY,
    FINALIZE_REVIEW_NOTICE,
    GRAPH_CONTEXT_POLICY,
    REVIEW_SEVERITY_CALIBRATION_GUIDANCE,
    SYSTEM_PROMPT_REVIEW,
    USER_PREFIX_REVIEW,
)


def test_finalize_review_notice_requires_empty_issues_for_speculative_suggestions() -> (
    None
):
    assert "speculative" in FINALIZE_REVIEW_NOTICE
    assert "info/style/design" in FINALIZE_REVIEW_NOTICE
    assert "issues: []" in FINALIZE_REVIEW_NOTICE


def test_review_prompts_call_out_silent_fallback_semantic_changes() -> None:
    combined = f"{SYSTEM_PROMPT_REVIEW}\n{FINALIZE_REVIEW_NOTICE}"

    assert "silent behavior change" in combined
    assert "fallback" in combined
    assert "coercion" in combined
    assert "exception handling" in combined
    assert "cross-type comparison" in combined
    assert "precision" in combined
    assert "error exposure" in combined
    assert "warning" in combined


def test_review_prompts_bound_positive_extra_to_independent_root_causes() -> None:
    combined = f"{SYSTEM_PROMPT_REVIEW}\n{FINALIZE_REVIEW_NOTICE}"
    normalized = combined.lower()

    assert "independent root cause" in combined
    assert "downstream symptom" in combined
    assert "same issue" in combined
    assert "hypothetical fix" in combined
    assert "do not promote" in normalized


def test_review_prompts_describe_atomic_review_tools() -> None:
    combined = f"{SYSTEM_PROMPT_REVIEW}\n{USER_PREFIX_REVIEW}"

    assert "get_changed_context" in combined
    assert "find_symbol_context" in combined
    assert "validate_review_draft" in combined
    assert "policy feedback" in combined
    assert "submit_review" in combined


def test_review_prompts_require_early_minimal_draft_without_promoting_it() -> None:
    combined = f"{SYSTEM_PROMPT_REVIEW}\n{USER_PREFIX_REVIEW}\n{FINALIZE_REVIEW_NOTICE}"

    assert "record_draft_finding" in combined
    assert "before further exploration" in combined
    assert "mandatory draft checkpoint" in combined
    assert "next assistant action must include" in combined
    assert "private analysis" in combined
    assert "optional line/symbol plus the claim only" in combined
    assert "investigation hypothesis" in combined
    assert "not a final finding" in combined
    assert "submit only those supported" in combined


def test_review_prompts_make_evidence_provenance_system_owned() -> None:
    assert "runtime binds candidate_id and retrieval_source" in AGENT_SEARCH_POLICY
    assert "runtime binds its real diff, read, symbol" in GRAPH_CONTEXT_POLICY
    assert "do not invent provenance metadata" in SYSTEM_PROMPT_REVIEW


def test_review_prompts_require_risk_severity_for_concrete_regressions() -> None:
    for prompt in (SYSTEM_PROMPT_REVIEW, USER_PREFIX_REVIEW, FINALIZE_REVIEW_NOTICE):
        normalized = prompt.lower()
        assert "must use warning or critical" in normalized
        assert "never info or style" in normalized
        assert "data loss" in normalized


def test_review_prompts_separate_impact_from_evidence_certainty() -> None:
    for prompt in (SYSTEM_PROMPT_REVIEW, USER_PREFIX_REVIEW, FINALIZE_REVIEW_NOTICE):
        assert REVIEW_SEVERITY_CALIBRATION_GUIDANCE in prompt

    normalized = REVIEW_SEVERITY_CALIBRATION_GUIDANCE.lower()
    assert "severity measures the impact" in normalized
    assert "confidence measures" in normalized
    assert "do not downgrade" in normalized
    assert "do not inflate confidence" in normalized
    assert "affected population is narrow" in normalized


def test_review_prompts_cover_compatibility_positive_and_negative_examples() -> None:
    normalized = REVIEW_SEVERITY_CALIBRATION_GUIDANCE.lower()

    assert "pre-change fallback" in normalized
    assert "author tests and pr intent" in normalized
    assert "wrapper unwrapping" in normalized
    assert "keying/grouping" in normalized
    assert "compatibility fallback" in normalized
    assert "wrapped value directly to its wrapper" in normalized
    assert "pure optimization with unchanged results is info" in normalized
    assert "depends only on a future caller" in normalized
