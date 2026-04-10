"""Analyzer-layer schemas for CLI and orchestrator integration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport


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
