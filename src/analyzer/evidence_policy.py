"""Mode-specific evidence-source contracts for the shared verifier core."""

from __future__ import annotations

from pydantic import BaseModel

from src.analyzer.context_mode import ReviewContextMode


class EvidencePolicy(BaseModel):
    require_manifest: bool = False
    allow_diff_evidence: bool = True
    allow_tool_evidence: bool = True
    allow_manifest_evidence: bool = False
    require_context_hash_for_manifest: bool = True


def evidence_policy_for_mode(mode: ReviewContextMode) -> EvidencePolicy:
    if mode == "agent_search":
        return EvidencePolicy()
    return EvidencePolicy(allow_manifest_evidence=True)
