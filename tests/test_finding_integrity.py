"""Offline contract tests for the thin finding integrity guard."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.analyzer.finding_integrity import FindingIntegrityGuard, build_candidates
from src.analyzer.finding_schema import EvidenceProvenance
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import AnalysisPlan, FindingCandidate, ReviewRequest
from src.orchestrator.agent_loop import AgentOrchestrator


def _request(tmp_path: Path) -> ReviewRequest:
    return ReviewRequest(
        repo_path=str(tmp_path),
        diff_mode=True,
        diff_text=(
            "diff --git a/pkg/service.py b/pkg/service.py\n"
            "--- a/pkg/service.py\n"
            "+++ b/pkg/service.py\n"
            "@@ -2,1 +2,1 @@\n"
            "-legacy_value = old()\n"
            "+current_value = new()\n"
        ),
    )


def _write_service(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "service.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def load():\n"
        "current_value = new()\n"
        "return current_value\n"
        "legacy_contract = True\n"
        "if legacy_contract:\n"
        "    return current_value\n"
        "supporting_behavior = True\n"
        "return supporting_behavior\n",
        encoding="utf-8",
    )


def _issue(
    *,
    location: str = "pkg/service.py:2",
    cause_line: int = 2,
    contract_file: str = "pkg/service.py",
    contract_line: int = 8,
    cause_source: str = "git_diff",
    contract_source: str = "read_file",
) -> ReviewIssue:
    return ReviewIssue(
        severity=Severity.WARNING,
        location=location,
        evidence="`current_value = new()` changes the returned behavior.",
        suggestion="Preserve the established contract for existing callers.",
        confidence=0.95,
        cause_evidence=[
            EvidenceProvenance(
                retrieval_source=cause_source,
                file="pkg/service.py",
                line=cause_line,
                statement="The PR changes the value produced by this path.",
            )
        ],
        contract_evidence=[
            EvidenceProvenance(
                retrieval_source=contract_source,
                file=contract_file,
                line=contract_line,
                statement="The unchanged code retains the existing caller contract.",
            )
        ],
    )


def _read_file_evidence(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "pkg" / "service.py").read_text(encoding="utf-8").splitlines()
    content = "\n".join(
        f"{index}: {line}" for index, line in enumerate(lines, start=1)
    )
    return [
        {
            "tool_name": "read_file",
            "arguments": {"file_path": "pkg/service.py", "offset": 0, "limit": 8},
            "data": {
                "file_path": "pkg/service.py",
                "start_line": 1,
                "line_count": 8,
                "content": content,
            },
        }
    ]


def _candidate(issue: ReviewIssue, request: ReviewRequest) -> FindingCandidate:
    return build_candidates(
        ReviewReport(summary="review", issues=[issue]),
        iteration=0,
    )[0]


def test_changed_anchor_with_unchanged_supporting_evidence_passes(tmp_path: Path) -> None:
    _write_service(tmp_path)
    request = _request(tmp_path)
    candidate = _candidate(_issue(), request)

    result = FindingIntegrityGuard(tmp_path).validate(
        [candidate],
        request,
        tool_evidence=_read_file_evidence(tmp_path),
        context_mode="agent_search",
    )

    assert result.passed_count == 1
    assert result.rejected_count == 0


def test_missing_repository_file_or_line_fails(tmp_path: Path) -> None:
    _write_service(tmp_path)
    request = _request(tmp_path)
    missing_file = _candidate(_issue(location="pkg/missing.py:2"), request)
    out_of_range = _candidate(_issue(location="pkg/service.py:99"), request)

    result = FindingIntegrityGuard(tmp_path).validate(
        [missing_file, out_of_range], request, context_mode="agent_search"
    )

    assert result.rejected_count == 2
    assert "repository_path_missing" in {
        item.code for item in result.results[0].failures
    }
    assert "location_line_out_of_range" in {
        item.code for item in result.results[1].failures
    }


def test_unobserved_supporting_evidence_fails(tmp_path: Path) -> None:
    _write_service(tmp_path)
    hidden = tmp_path / "pkg" / "hidden.py"
    hidden.write_text("contract = True\n", encoding="utf-8")
    request = _request(tmp_path)
    candidate = _candidate(
        _issue(contract_file="pkg/hidden.py", contract_line=1), request
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        [candidate], request, context_mode="agent_search"
    )

    assert result.rejected_count == 1
    codes = {item.code for item in result.results[0].failures}
    assert "evidence_not_observed" in codes


def test_finding_without_any_changed_anchor_fails(tmp_path: Path) -> None:
    _write_service(tmp_path)
    request = _request(tmp_path)
    candidate = _candidate(
        _issue(location="pkg/service.py:8", cause_line=8), request
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        [candidate],
        request,
        tool_evidence=_read_file_evidence(tmp_path),
        context_mode="agent_search",
    )

    assert result.rejected_count == 1
    assert "changed_anchor_missing" in {
        item.code for item in result.results[0].failures
    }


def test_candidate_and_evidence_binding_mismatch_fails(tmp_path: Path) -> None:
    _write_service(tmp_path)
    request = _request(tmp_path)
    issue = _issue().model_copy(
        update={
            "cause_evidence": [
                EvidenceProvenance(
                    candidate_id="candidate-b",
                    retrieval_source="git_diff",
                    file="pkg/service.py",
                    line=2,
                    statement="Changed code.",
                )
            ]
        }
    )
    candidate = FindingCandidate(
        candidate_id="candidate-a",
        issue=issue,
        claim=issue.suggestion,
        originating_iteration=0,
    )

    result = FindingIntegrityGuard(tmp_path).validate(
        [candidate], request, context_mode="agent_search"
    )

    assert result.rejected_count == 1
    assert "candidate_binding_mismatch" in {
        item.code for item in result.results[0].failures
    }


def test_ordinary_changed_code_finding_passes(tmp_path: Path) -> None:
    _write_service(tmp_path)
    request = _request(tmp_path)
    issue = ReviewIssue(
        severity=Severity.WARNING,
        location="pkg/service.py:2",
        evidence="`current_value = new()` is a concrete changed line.",
        suggestion="Preserve the established caller behavior.",
        confidence=0.95,
    )
    candidate = _candidate(issue, request)

    result = FindingIntegrityGuard(tmp_path).validate(
        [candidate], request, context_mode="agent_search"
    )

    assert result.passed_count == 1


def test_default_orchestrator_uses_integrity_guard_without_semantic_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    _write_service(tmp_path)
    monkeypatch.setenv("ROOT_CAUSE_CONSOLIDATION_ENABLED", "false")
    orchestrator = AgentOrchestrator(
        review_workflow_enforcement="off",
        review_diff_first_changed_files=False,
    )
    issue = ReviewIssue(
        severity=Severity.WARNING,
        location="pkg/service.py:2",
        evidence="`current_value = new()` is a concrete changed line.",
        suggestion="Preserve the established caller behavior.",
        confidence=0.95,
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(draft_review=ReviewReport(summary="review", issues=[issue]))

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(orchestrator.run_review(_request(tmp_path)))

    assert len(response.report.issues) == 1
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    verification = next(
        event
        for event in events
        if event["event_type"] == "finding_verification_completed"
    )
    assert verification["payload"]["verifier_kind"] == "integrity_guard"
