"""Tests for model prompt contracts."""

from src.analyzer.prompts import FINALIZE_REVIEW_NOTICE, SYSTEM_PROMPT_REVIEW


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
