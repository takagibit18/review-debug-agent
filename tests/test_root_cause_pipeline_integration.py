"""End-to-end review pipeline coverage for v0.2.3-v0.2.5."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RepairIntent,
    SourceAnchor,
)
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import (
    AnalysisPlan,
    FindingVerification,
    FindingVerificationBatch,
    ReviewRequest,
)
from src.orchestrator.agent_loop import AgentOrchestrator


class _AcceptingVerifier:
    async def verify(  # type: ignore[no-untyped-def]
        self, candidates, request, state, *, tool_evidence=None
    ):
        del request, state, tool_evidence
        return FindingVerificationBatch(
            results=[
                FindingVerification(
                    candidate_id=item.candidate_id,
                    status="accepted",
                    reason_codes=["verified"],
                    rationale="The manifest-bound code supports the hypothesis.",
                    verified_evidence=[item.issue.location],
                )
                for item in candidates
            ]
        )


def test_structured_review_is_verified_consolidated_and_verified_again(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "safe_hash.py"
    source.write_text(
        "class SafeHashWrapper:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "    def __eq__(self, other):\n"
        "        return self.value == other.value\n"
        "    def __hash__(self):\n"
        "        return hash(id(self.value))\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/safe_hash.py b/safe_hash.py\n"
        "--- /dev/null\n"
        "+++ b/safe_hash.py\n"
        "@@ -0,0 +1,7 @@\n"
        "+class SafeHashWrapper:\n"
        "+    def __init__(self, value):\n"
        "+        self.value = value\n"
        "+    def __eq__(self, other):\n"
        "+        return self.value == other.value\n"
        "+    def __hash__(self):\n"
        "+        return hash(id(self.value))\n"
    )
    orchestrator = AgentOrchestrator(
        finding_verifier=_AcceptingVerifier(),
        finding_verifier_mode="enforce",
        review_workflow_enforcement="off",
        review_max_iterations=1,
    )

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        del request, tool_specs, kwargs
        assert state.candidate_context_manifests
        manifest = state.candidate_context_manifests[0]
        changed_span = next(
            item
            for item in manifest["included_spans"]
            if item["role"] == "changed_hunk"
        )

        def evidence(line: int, statement: str) -> EvidenceProvenance:
            return EvidenceProvenance(
                candidate_id=manifest["candidate_id"],
                context_manifest_id=manifest["candidate_id"],
                retrieval_source=changed_span["retrieval_source"],
                file="safe_hash.py",
                line=line,
                symbol_id=changed_span["symbol_id"],
                context_hash=changed_span["context_hash"],
                resolver="git_diff",
                statement=statement,
            )

        def issue(finding_id: str, line: int, observation: str) -> ReviewIssue:
            cause = evidence(line, observation)
            contract = evidence(line, "Equal values must produce equal hashes.")
            return ReviewIssue(
                severity=Severity.WARNING,
                location=f"safe_hash.py:{line}",
                evidence=observation,
                suggestion="Derive equality and hashing from the same stable value.",
                confidence=0.96,
                schema_version="2.0",
                finding_id=finding_id,
                primary_anchor=SourceAnchor(
                    file="safe_hash.py",
                    line=line,
                    symbol_id=changed_span["symbol_id"],
                ),
                observed_behavior=observation,
                causal_mechanism=(
                    "Equality compares wrapped values while hashing uses object identity"
                ),
                violated_invariant="Equal wrappers must have identical hashes",
                repair_intent=RepairIntent(
                    action="Derive equality and hash from the same stable value",
                    targets=[
                        "SafeHashWrapper.__eq__",
                        "SafeHashWrapper.__hash__",
                    ],
                    boundary="SafeHashWrapper equality/hash contract",
                ),
                cause_evidence=[cause],
                contract_evidence=[contract],
                context_manifest_id=manifest["candidate_id"],
            )

        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="Two observations share one equality/hash repair unit.",
                issues=[
                    issue("F-EQUALITY", 5, "Equality is based on wrapped value"),
                    issue("F-HASH", 7, "Hashing is based on wrapped object identity"),
                ],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=diff,
            )
        )
    )

    assert len(response.report.issues) == 1
    root = response.report.issues[0]
    assert root.root_cause_id.startswith("RC-")
    assert root.member_findings == ["F-EQUALITY", "F-HASH"]
    assert root.counterfactual_result == "yes"
    assert root.primary_anchor is not None
    assert root.primary_anchor.line in {5, 7}
    retained_lines = {root.primary_anchor.line} | {
        location.line for location in root.related_locations
    }
    assert {5, 7}.issubset(retained_lines)
    assert response.context.candidate_context_manifests

    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    event_types = {
        json.loads(line)["event_type"]
        for line in log_path.read_text(encoding="utf-8").splitlines()
    }
    assert "context_manifest_created" in event_types
    assert "finding_verification_completed" in event_types
    assert "consolidation_verification_completed" in event_types
    assert "root_cause_consolidation_completed" in event_types
