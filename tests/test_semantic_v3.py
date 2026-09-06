"""Strict semantic-v3 evaluation matcher contracts."""

from __future__ import annotations

from eval.runner import _match_issues, _match_issues_for_version
from eval.schemas import DEFAULT_EVAL_MATCHER_VERSION, Fixture
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewResponse


def _fixture(*, structured: bool = True) -> Fixture:
    expected = {
        "severity": "warning",
        "location_pattern": "src/_pytest/fixtures.py",
        "path": "src/_pytest/fixtures.py" if structured else "",
        "line": 244 if structured else None,
        "end_line": 250 if structured else None,
    }
    return Fixture.model_validate(
        {
            "id": "pytest-9350",
            "type": "review",
            "source": {"repo_full_name": "pytest-dev/pytest", "pr_number": 9350},
            "input": {"diff_text": "", "files": {}},
            "expected": {"issues": [expected]},
        }
    )


def _response(location: str) -> ReviewResponse:
    return ReviewResponse(
        run_id="run",
        report=ReviewReport(
            summary="found the wrapper equality regression",
            issues=[
                ReviewIssue(
                    severity=Severity.WARNING,
                    location=location,
                    evidence="SafeHashWrapper.__eq__ compares the wrapper directly.",
                    suggestion="Compare other.obj when both operands are wrappers.",
                    confidence=0.95,
                )
            ],
        ),
        context=ContextState(),
    )


def test_semantic_v3_does_not_use_filename_pattern_to_bypass_line_range() -> None:
    fixture = _fixture()
    response = _response("src/_pytest/fixtures.py:253-256")

    legacy = _match_issues(fixture, response)
    strict = _match_issues_for_version(fixture, response, "semantic-v3")

    assert legacy[1:] == (1, 0)
    assert strict[1:] == (0, 1)


def test_semantic_v3_matches_an_overlapping_structured_span() -> None:
    fixture = _fixture()
    response = _response("src/_pytest/fixtures.py:246-251")

    matches, matched_count, false_positive_count = _match_issues_for_version(
        fixture, response, DEFAULT_EVAL_MATCHER_VERSION
    )

    assert matches[0].matched is True
    assert matched_count == 1
    assert false_positive_count == 0


def test_semantic_v3_keeps_pattern_only_legacy_fixture_compatibility() -> None:
    fixture = _fixture(structured=False)
    response = _response("src/_pytest/fixtures.py:244-250")

    _, matched_count, false_positive_count = _match_issues_for_version(
        fixture, response, "semantic-v3"
    )

    assert matched_count == 1
    assert false_positive_count == 0
