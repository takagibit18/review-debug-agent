"""Tests for model prompt contracts."""

from src.analyzer.prompts import FINALIZE_REVIEW_NOTICE, SYSTEM_PROMPT_REVIEW, USER_PREFIX_REVIEW


def test_finalize_review_notice_requires_empty_issues_for_speculative_suggestions() -> None:
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
