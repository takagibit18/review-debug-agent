"""Analyzer-layer schemas for CLI and orchestrator integration."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.analyzer.context_state import ContextState
from src.analyzer.finding_schema import normalize_repo_path
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, ReviewReport


class ReviewRequest(BaseModel):
    """Structured input for a review run."""

    repo_path: str = Field(
        ...,
        description="Target repository or directory path",
    )
    diff_mode: bool = Field(
        default=False,
        description="Whether to run in diff mode",
    )
    diff_text: str | None = Field(
        default=None,
        description="Optional diff text input",
    )
    model_name: str | None = Field(
        default=None,
        description="Model override from CLI",
    )
    verbose: bool = Field(
        default=False,
        description="Whether verbose output is enabled",
    )


class DebugRequest(BaseModel):
    """Structured input for a debug run."""

    repo_path: str = Field(
        ...,
        description="Target repository or directory path",
    )
    error_log_path: str | None = Field(
        default=None,
        description="Optional error log path",
    )
    error_log_text: str | None = Field(
        default=None,
        description="Optional error log content",
    )
    model_name: str | None = Field(
        default=None,
        description="Model override from CLI",
    )
    verbose: bool = Field(
        default=False,
        description="Whether verbose output is enabled",
    )


class ReviewResponse(BaseModel):
    """Structured output for a review run."""

    run_id: str = Field(
        ...,
        description="Unique identifier for the current run",
    )
    report: ReviewReport = Field(
        ...,
        description="Structured review report",
    )
    context: ContextState = Field(
        ...,
        description="Session context for audit and debugging",
    )
    workflow_invalid: bool = Field(
        default=False,
        description="Whether required review workflow steps remained incomplete.",
    )
    workflow_missing_steps: list[str] = Field(
        default_factory=list,
        description="Required workflow steps that remained incomplete.",
    )


class DebugStep(BaseModel):
    """A single debug step in the structured debug response."""

    title: str = Field(
        ...,
        description="Short title for the debug step",
    )
    detail: str = Field(
        ...,
        description="Detailed explanation of the step",
    )
    location: str = Field(
        default="",
        description="Relevant file location or code reference",
    )
    evidence: str = Field(
        default="",
        description="Evidence supporting this step",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score",
    )


class SuggestedCommand(BaseModel):
    """A suggested command that the user may choose to run."""

    command: str = Field(
        ...,
        description="Suggested shell command",
    )
    rationale: str = Field(
        ...,
        description="Why this command is suggested",
    )
    risk: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Risk level of the suggested command",
    )


class DebugResponse(BaseModel):
    """Structured output for a debug run."""

    run_id: str = Field(
        ...,
        description="Unique identifier for the current run",
    )
    summary: str = Field(
        ...,
        description="High-level debug summary",
    )
    hypotheses: list[str] = Field(
        default_factory=list,
        description="Candidate root-cause hypotheses",
    )
    steps: list[DebugStep] = Field(
        default_factory=list,
        description="Suggested debug steps",
    )
    suggested_commands: list[SuggestedCommand] = Field(
        default_factory=list,
        description="Commands suggested for manual execution",
    )
    suggested_patch: str | None = Field(
        default=None,
        description="Optional suggested patch",
    )
    context: ContextState = Field(
        ...,
        description="Session context for audit and debugging",
    )


class AnalysisPlan(BaseModel):
    """Structured plan produced by the analyze phase."""

    needs_tools: bool = Field(
        default=False,
        description="Whether tool execution is required in this iteration",
    )
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw tool-call payloads parsed from model output",
    )
    draft_review: ReviewReport | None = Field(
        default=None,
        description="Optional draft review result produced by model",
    )
    draft_debug: DebugResponse | None = Field(
        default=None,
        description="Optional draft debug result produced by model",
    )
    incomplete_reason: str = Field(
        default="",
        description="Reason the model response was unusable for a trusted final result.",
    )
    model_finish_reason: str = Field(
        default="",
        description="Provider finish reason retained for runtime and funnel telemetry.",
    )
    final_submit_evidence_included_count: int = Field(
        default=0,
        ge=0,
        description="Number of deduplicated evidence entries retained for final submit.",
    )
    final_submit_evidence_token_count: int = Field(
        default=0,
        ge=0,
        description="Estimated tokens used by the bounded final-submit evidence digest.",
    )
    final_submit_evidence_truncated_count: int = Field(
        default=0,
        ge=0,
        description="Evidence entries omitted or shortened to respect the digest budget.",
    )


FindingVerificationStatus = Literal[
    "accepted",
    "rejected",
    "needs_evidence",
    "downgraded",
]
FindingCandidateKind = Literal[
    "risk",
    "filter_rescue",
    "severity_calibration",
]
FindingVerificationReason = Literal[
    "evidence_not_found",
    "deterministic_evidence_invalid",
    "claim_not_supported",
    "not_introduced_by_diff",
    "cross_file_context_missing",
    "suggestion_not_actionable",
    "severity_overstated",
    "duplicate_finding",
    "observed_behavior_unsupported",
    "causal_mechanism_unsupported",
    "manifest_evidence_missing",
    "low_confidence_graph_evidence",
    "unreceived_context_claim",
    "verified",
]

_LEGACY_VERIFIED_LOCATION = re.compile(r"^[^\s:]+:\d+(?:-\d+)?$")


class VerifiedEvidenceLocation(BaseModel):
    """One verifier-confirmed repository location, separate from its rationale."""

    file: str = Field(
        min_length=1,
        description="Repository-relative source path using forward slashes.",
    )
    line: int = Field(ge=1, description="One-based start line.")
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional inclusive one-based end line.",
    )

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_canonical_location(cls, value: Any) -> Any:
        """Read only strict legacy path:line spans; never mine prose for a location."""

        if not isinstance(value, str):
            return value
        raw = value.strip()
        if _LEGACY_VERIFIED_LOCATION.fullmatch(raw) is None:
            raise ValueError(
                "legacy verified evidence must be an exact path:line[-end_line]"
            )
        parsed = normalize_location(raw)
        if not parsed.valid or parsed.path is None or parsed.line is None:
            raise ValueError("legacy verified evidence is not a repo-relative location")
        return {
            "file": parsed.path,
            "line": parsed.line,
            "end_line": parsed.end_line,
        }

    @model_validator(mode="after")
    def _validate_repo_location(self) -> "VerifiedEvidenceLocation":
        normalized_file = normalize_repo_path(self.file)
        suffix = str(self.line)
        if self.end_line is not None:
            suffix += f"-{self.end_line}"
        parsed = normalize_location(f"{normalized_file}:{suffix}")
        if (
            not parsed.valid
            or parsed.path is None
            or parsed.line is None
            or parsed.path != normalized_file
        ):
            raise ValueError("verified evidence file must be repository-relative")
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError("end_line must be greater than or equal to line")
        self.file = normalized_file
        return self

    @property
    def location(self) -> str:
        suffix = str(self.line)
        if self.end_line is not None and self.end_line != self.line:
            suffix += f"-{self.end_line}"
        return f"{self.file}:{suffix}"


class FindingCandidate(BaseModel):
    """One risk finding awaiting independent semantic verification."""

    candidate_id: str
    issue: ReviewIssue
    claim: str
    evidence_locations: list[str] = Field(default_factory=list)
    originating_iteration: int = Field(ge=0)
    candidate_kind: FindingCandidateKind = "risk"
    source_issue_index: int = Field(default=0, ge=0)


class FindingVerification(BaseModel):
    """Independent verdict for one candidate finding."""

    candidate_id: str
    status: FindingVerificationStatus
    reason_codes: list[FindingVerificationReason] = Field(default_factory=list)
    rationale: str
    verified_evidence: list[VerifiedEvidenceLocation] = Field(
        default_factory=list,
        description=(
            "Structured code locations verified against supplied candidate context; "
            "semantic explanation belongs in rationale."
        ),
    )
    revised_issue: ReviewIssue | None = None


class FindingVerificationBatch(BaseModel):
    """Structured verifier result for a candidate batch."""

    results: list[FindingVerification] = Field(default_factory=list)
