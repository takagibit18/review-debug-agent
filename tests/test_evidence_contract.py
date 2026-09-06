"""Evidence-role completeness and deterministic recovery contracts."""

from __future__ import annotations

from pathlib import Path

from src.analyzer.evidence_binding import bind_candidate_evidence
from src.analyzer.finding_integrity import FindingIntegrityGuard, build_candidates
from src.analyzer.finding_schema import EvidenceProvenance, RepairIntent, SourceAnchor
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import ReviewRequest


def _request(repo_path: str) -> ReviewRequest:
    return ReviewRequest(
        repo_path=repo_path,
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n"
            "+++ b/pkg/service.py\n"
            "@@ -2 +2 @@\n"
            "-return old_value\n"
            "+return new_value\n"
        ),
    )


def _read_evidence() -> list[dict[str, object]]:
    return [
        {
            "tool_name": "read_file",
            "arguments": {"file_path": "pkg/service.py"},
            "data": {
                "file_path": "pkg/service.py",
                "start_line": 1,
                "line_count": 6,
                "content": "1: def load():\n2: return new_value\n3: return new_value\n4: contract = True\n5: return contract\n6: end = True",
            },
        }
    ]


def _structured_issue(*, contract: bool = True) -> ReviewIssue:
    return ReviewIssue(
        severity=Severity.WARNING,
        location="pkg/service.py:2",
        evidence="The changed return violates the established contract.",
        suggestion="Preserve the established caller contract.",
        confidence=0.95,
        schema_version="2.0",
        primary_anchor=SourceAnchor(file="pkg/service.py", line=2),
        observed_behavior="The returned value changes unexpectedly.",
        causal_mechanism="The changed producer returns a value with different semantics.",
        violated_invariant="Callers receive the established value contract.",
        repair_intent=RepairIntent(action="Restore the established return value"),
        cause_evidence=[
            EvidenceProvenance(
                retrieval_source="git_diff",
                file="pkg/service.py",
                line=2,
                statement="The changed return produces the new value.",
            )
        ],
        contract_evidence=(
            [
                EvidenceProvenance(
                    retrieval_source="read_file",
                    file="pkg/service.py",
                    line=4,
                    statement="The unchanged contract remains established here.",
                )
            ]
            if contract
            else []
        ),
    )


def test_structured_risk_requires_cause_and_contract_roles(tmp_path: Path) -> None:
    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("\n".join(f"line {line}" for line in range(1, 7)) + "\n")
    request = _request(str(tmp_path))
    candidate = build_candidates(
        ReviewReport(issues=[_structured_issue(contract=False)]), iteration=0
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        candidate,
        request,
        tool_evidence=_read_evidence(),
        context_mode="agent_search",
    )

    assert result.rejected_count == 1
    assert result.bound_candidates[0].verification_status == "verification_blocked"
    assert {
        failure.code for failure in result.results[0].failures
    } >= {"evidence_incomplete"}


def test_invalid_optional_role_evidence_is_dropped_when_required_roles_pass(
    tmp_path: Path,
) -> None:
    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("\n".join(f"line {line}" for line in range(1, 7)) + "\n")
    issue = _structured_issue()
    issue.trigger_evidence = [
        EvidenceProvenance(
            retrieval_source="read_file",
            file="pkg/missing.py",
            line=1,
            statement="This optional trigger was not observed.",
        )
    ]
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    result = FindingIntegrityGuard(tmp_path).validate(
        candidate,
        _request(str(tmp_path)),
        tool_evidence=_read_evidence(),
        context_mode="agent_search",
    )

    assert result.passed_count == 1
    assert result.bound_candidates[0].issue.trigger_evidence == []


def test_invalid_declared_trigger_evidence_blocks_required_role(
    tmp_path: Path,
) -> None:
    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("\n".join(f"line {line}" for line in range(1, 7)) + "\n")
    issue = _structured_issue()
    issue.trigger = "The changed value reaches the wrong consumer."
    issue.trigger_evidence = [
        EvidenceProvenance(
            retrieval_source="read_file",
            file="pkg/missing.py",
            line=1,
            statement="The consumer receives the changed value.",
        )
    ]
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    result = FindingIntegrityGuard(tmp_path).validate(
        candidate,
        _request(str(tmp_path)),
        tool_evidence=_read_evidence(),
        context_mode="agent_search",
    )

    assert result.rejected_count == 1
    assert "evidence_incomplete" in {
        failure.code for failure in result.results[0].failures
    }


def test_unique_observed_source_rebinds_unknown_source_deterministically(
    tmp_path: Path,
) -> None:
    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("\n".join(f"line {line}" for line in range(1, 7)) + "\n")
    issue = _structured_issue()
    issue.contract_evidence[0].retrieval_source = "model_invented_source"
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    bound = bind_candidate_evidence(
        candidate,
        _request(str(tmp_path)),
        _read_evidence(),
    )

    assert bound[0].issue.contract_evidence[0].retrieval_source == "read_file"


def test_explicit_hash_without_manifest_id_remains_fail_closed(tmp_path: Path) -> None:
    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("\n".join(f"line {line}" for line in range(1, 7)) + "\n")
    issue = _structured_issue()
    issue.contract_evidence[0].context_hash = "model-invented-hash"
    candidate = build_candidates(ReviewReport(issues=[issue]), iteration=0)

    result = FindingIntegrityGuard(tmp_path).validate(
        candidate,
        _request(str(tmp_path)),
        tool_evidence=_read_evidence(),
        context_mode="agent_search",
    )

    assert result.rejected_count == 1
    assert "evidence_not_observed" in {
        failure.code for failure in result.results[0].failures
    }
