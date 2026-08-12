"""Contract tests for structured semantic-verifier evidence locations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.analyzer.finding_verifier import build_candidates, validate_verifications
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import (
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
    VerifiedEvidenceLocation,
)


def _verdict(*locations: object) -> FindingVerification:
    return FindingVerification(
        candidate_id="candidate",
        status="accepted",
        reason_codes=["verified"],
        rationale="The supplied code supports the claim.",
        verified_evidence=list(locations),
    )


def test_verified_evidence_uses_structured_single_line_location() -> None:
    verdict = _verdict({"file": "pkg/service.py", "line": 12})

    assert verdict.verified_evidence == [
        VerifiedEvidenceLocation(file="pkg/service.py", line=12)
    ]
    assert verdict.verified_evidence[0].location == "pkg/service.py:12"


def test_verified_evidence_supports_ranges_and_normalizes_repo_relative_path() -> None:
    verdict = _verdict({"file": ".\\pkg\\service.py", "line": 12, "end_line": 16})

    assert verdict.verified_evidence[0].file == "pkg/service.py"
    assert verdict.verified_evidence[0].location == "pkg/service.py:12-16"


def test_strict_legacy_location_is_read_into_structured_form() -> None:
    verdict = _verdict("pkg/service.py:12-16")

    assert verdict.verified_evidence == [
        VerifiedEvidenceLocation(file="pkg/service.py", line=12, end_line=16)
    ]


@pytest.mark.parametrize(
    "invalid_location",
    [
        "tests/test_xxx.py around line 335 proves this behavior",
        "tests/test_xxx.py:335 proves this behavior",
        {"line": 335},
        {"file": "tests/test_xxx.py"},
        {"file": "../tests/test_xxx.py", "line": 335},
    ],
)
def test_prose_missing_fields_and_non_repo_paths_are_rejected(
    invalid_location: object,
) -> None:
    with pytest.raises(ValidationError):
        _verdict(invalid_location)


def test_verifier_tool_schema_requires_location_objects() -> None:
    schema = FindingVerificationBatch.model_json_schema()
    location_schema = schema["$defs"]["VerifiedEvidenceLocation"]

    assert location_schema["type"] == "object"
    assert set(location_schema["required"]) == {"file", "line"}


def test_nonexistent_structured_verified_location_fails_closed() -> None:
    issue = ReviewIssue(
        severity=Severity.WARNING,
        location="pkg/service.py:12",
        evidence="`return cache[key]` reads the cache before population.",
        suggestion="Populate the cache before reading it.",
        confidence=0.95,
    )
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)[0]
    batch = FindingVerificationBatch(
        results=[
            FindingVerification(
                candidate_id=candidate.candidate_id,
                status="accepted",
                reason_codes=["verified"],
                rationale="The cited location was not actually supplied.",
                verified_evidence=[{"file": "pkg/missing.py", "line": 99}],
            )
        ]
    )
    request = ReviewRequest(
        repo_path=".",
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n"
            "+++ b/pkg/service.py\n"
            "@@ -11,0 +12,1 @@\n"
            "+return cache[key]\n"
        ),
    )

    result = validate_verifications([candidate], batch, request)

    assert result.results[0].status == "rejected"
    assert result.results[0].reason_codes == ["deterministic_evidence_invalid"]
