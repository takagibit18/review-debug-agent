"""Offline GLM-shaped replay checks for semantic-v2 and semantic-v3."""

from __future__ import annotations

from eval.runner import (
    _match_issues,
    _match_issues_for_version,
    _root_cause_quality_for_version,
    load_fixtures,
)
from eval.schemas import DEFAULT_EVAL_MATCHER_VERSION
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse


def _glm53_response() -> ReviewResponse:
    return ReviewResponse(
        run_id="offline-glm53-replay",
        report=ReviewReport(
            summary="The wrapper can raise while generating fixture reorder keys.",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location="src/_pytest/fixtures.py:253-256",
                    evidence="SafeHashWrapper.__hash__ delegates to hash(self.obj).",
                    suggestion="Fall back to identity hashing when delegation raises.",
                    confidence=0.85,
                )
            ],
        ),
        context=ContextState(),
    )


def test_existing_pytest_golden_replay_is_strict_by_default() -> None:
    fixture = next(
        item
        for item in load_fixtures(reviewed_only=True)
        if item.id == "golden_pytest-dev_pytest_pr9350"
    )
    response = _glm53_response()

    assert _match_issues(fixture, response)[1:] == (1, 0)
    assert _match_issues_for_version(
        fixture, response, DEFAULT_EVAL_MATCHER_VERSION
    )[1:] == (0, 1)


def test_location_only_golden_does_not_infer_root_cause_metrics() -> None:
    fixture = next(
        item
        for item in load_fixtures(reviewed_only=True)
        if item.id == "golden_pytest-dev_pytest_pr9350"
    )
    response = _glm53_response()
    matches = _match_issues_for_version(
        fixture, response, DEFAULT_EVAL_MATCHER_VERSION
    )[0]

    quality = _root_cause_quality_for_version(
        fixture, response, matches, DEFAULT_EVAL_MATCHER_VERSION
    )

    assert quality["expected_root_cause_count"] is None
    assert quality["matched_root_cause_count"] is None
    assert quality["repair_unit_expected_count"] is None
