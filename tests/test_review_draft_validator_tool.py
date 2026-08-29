"""Tests for deterministic review draft validation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.review_context import ReviewToolContext
from src.tools.review_draft_validator_tool import ValidateReviewDraftTool


def _context(tmp_path: Path) -> ReviewToolContext:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def run():\n"
        "-    return 'old'\n"
        "+    return 'new'\n"
    )
    return ReviewToolContext.from_diff(tmp_path, diff)


def test_validate_review_draft_uses_current_filter_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tool = ValidateReviewDraftTool(_context(tmp_path))

    result = asyncio.run(
        tool.execute(
            summary="Review found a regression.",
            issues=[
                {
                    "severity": "critical",
                    "location": "src/app.py:2",
                    "evidence": "+    return 'new'",
                    "suggestion": "Restore old behavior.",
                    "confidence": 0.84,
                    "cause_evidence": [
                        {
                            "retrieval_source": "git_diff",
                            "file": "src/app.py",
                            "line": 2,
                            "statement": "The return value changed here.",
                        }
                    ],
                },
                {
                    "severity": "warning",
                    "location": "src/app.py:2",
                    "evidence": "+    return 'new'",
                    "suggestion": "This user-visible behavior change can break callers.",
                    "confidence": 0.75,
                    "cause_evidence": [
                        {
                            "retrieval_source": "git_diff",
                            "file": "src/app.py",
                            "line": 2,
                            "statement": "The return value changed here.",
                        }
                    ],
                },
            ],
        )
    )

    assert result["issue_results"][0]["passes_current_filter"] is False
    assert any(
        "do not inflate confidence" in hint
        for hint in result["issue_results"][0]["repair_hints"]
    )
    assert result["issue_results"][1]["passes_current_filter"] is True
    assert result["issue_results"][1]["filter_reason_codes"] == [
        "warning_confidence_below_standard_threshold",
        "warning_relaxed_risk_policy_passed",
    ]
    assert result["issue_results"][1]["standard_threshold"] == 0.85
    assert result["issue_results"][1]["relaxed_threshold"] == 0.70
    assert result["issue_results"][1]["risk_pattern_matched"] is True
    assert result["effective_issue_count"] == 1


def test_validate_review_draft_separates_display_location_from_causal_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tool = ValidateReviewDraftTool(_context(tmp_path))

    result = asyncio.run(
        tool.execute(
            summary="One warning.",
            issues=[
                {
                    "severity": "warning",
                    "location": ".\\src\\app.py:1",
                    "evidence": "+    return 'new'",
                    "suggestion": "Check behavior.",
                    "confidence": 0.9,
                    "cause_evidence": [
                        {
                            "retrieval_source": "git_diff",
                            "file": "src/app.py",
                            "line": 2,
                            "statement": "The changed return causes the issue.",
                        }
                    ],
                }
            ],
        )
    )

    issue = result["issue_results"][0]
    assert issue["normalized_location"] == "src/app.py:1"
    assert issue["location_valid"] is True
    assert issue["location_on_changed_line"] is False
    assert issue["pr_causal_anchor_on_changed_line"] is True
    assert issue["passes_current_filter"] is True
    assert not any("move location" in hint for hint in issue["repair_hints"])


def test_validate_review_draft_accepts_changed_display_anchor_without_changed_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tool = ValidateReviewDraftTool(_context(tmp_path))

    result = asyncio.run(
        tool.execute(
            summary="One warning.",
            issues=[
                {
                    "severity": "warning",
                    "location": "src/app.py:2",
                    "evidence": "+    return 'new'",
                    "suggestion": "Preserve caller behavior.",
                    "confidence": 0.9,
                }
            ],
        )
    )

    issue = result["issue_results"][0]
    assert issue["location_on_changed_line"] is True
    assert issue["pr_causal_anchor_on_changed_line"] is False
    assert issue["changed_anchor_present"] is True
    assert issue["passes_current_filter"] is True
    assert "changed_anchor_missing" not in issue["fail_reasons"]


def test_validate_review_draft_warns_when_summary_mentions_regression_without_surviving_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tool = ValidateReviewDraftTool(_context(tmp_path))

    result = asyncio.run(
        tool.execute(
            summary="This introduces a regression in app behavior.",
            issues=[
                {
                    "severity": "warning",
                    "location": "src/app.py:2",
                    "evidence": "Looks suspicious.",
                    "suggestion": "Investigate.",
                    "confidence": 0.4,
                }
            ],
        )
    )

    assert result["effective_issue_count"] == 0
    assert result["should_submit_empty_issues"] is False
    assert result["summary_warnings"] == [
        "summary mentions bug/regression/breaking/user-visible risk but no issue passes current filter"
    ]


def test_validate_review_draft_keeps_info_and_style_as_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tool = ValidateReviewDraftTool(_context(tmp_path))

    result = asyncio.run(
        tool.execute(
            summary="Style-only comments.",
            issues=[
                {
                    "severity": "style",
                    "location": "src/app.py:1",
                    "evidence": "Formatting is inconsistent.",
                    "suggestion": "Run formatter.",
                    "confidence": 0.2,
                }
            ],
        )
    )

    issue = result["issue_results"][0]
    assert issue["passes_current_filter"] is True
    assert issue["location_on_changed_line"] is False
    assert issue["fail_reasons"] == []
