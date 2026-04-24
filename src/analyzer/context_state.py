"""Structured state management for review / debug sessions.

Tracks the evolving context — goal, constraints, decisions, files under
inspection, and accumulated errors — so that the agent loop and tools can
make informed decisions at each phase.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DecisionStep(BaseModel):
    """A single reasoning or decision record within a session."""

    phase: str = Field(..., description="Which agent phase produced this decision")
    action: str = Field(..., description="What was decided or executed")
    result: str = Field(default="", description="Outcome or observation")


class ErrorDetail(BaseModel):
    """Structured representation of an error encountered during analysis."""

    file: str = Field(default="", description="File path where the error relates to")
    line: int | None = Field(default=None, description="Line number, if applicable")
    message: str = Field(..., description="Human-readable error description")
    category: str = Field(
        default="unknown",
        description="Error category: syntax | runtime | logic | style | security",
    )


class RunDiagnostics(BaseModel):
    """Structured end-of-run diagnostics surfaced to CLI/JSON consumers."""

    status: Literal["completed", "partial", "degraded"] = Field(
        default="completed",
        description="Overall run quality from the user's perspective.",
    )
    stop_reason: str = Field(
        default="",
        description="Final orchestrator stop reason such as model_completed or budget_soft_capped.",
    )
    budget_state: Literal["none", "soft_capped", "hard_capped"] = Field(
        default="none",
        description="Budget state observed at stop time.",
    )
    blocking_tool_error: bool = Field(
        default=False,
        description="Whether any tool call failed during the run.",
    )
    tool_error_count: int = Field(
        default=0,
        ge=0,
        description="Count of failed tool calls observed across iterations.",
    )
    error_count: int = Field(
        default=0,
        ge=0,
        description="Count of structured errors accumulated in ContextState.",
    )
    used_placeholder_summary: bool = Field(
        default=False,
        description="Whether the final user-visible summary is a placeholder fallback.",
    )
    finalize_attempted: bool = Field(
        default=False,
        description="Whether the orchestrator performed a finalize-only retry.",
    )
    finalize_submit_seen: bool = Field(
        default=False,
        description="Whether the finalize-only retry produced a valid submit payload.",
    )
    submit_review_seen: bool = Field(
        default=False,
        description="Whether the model attempted submit_review at least once.",
    )
    submit_debug_seen: bool = Field(
        default=False,
        description="Whether the model attempted submit_debug at least once.",
    )
    submit_review_validation_error: str = Field(
        default="",
        description="Validation error from the latest submit_review attempt, if any.",
    )
    submit_debug_validation_error: str = Field(
        default="",
        description="Validation error from the latest submit_debug attempt, if any.",
    )
    fallback_json_found: bool = Field(
        default=False,
        description="Whether fallback JSON was detected in assistant text output.",
    )
    fallback_parse_valid: bool = Field(
        default=False,
        description="Whether fallback JSON could be parsed into the expected schema.",
    )
    fallback_plan_used: bool = Field(
        default=False,
        description="Whether the orchestrator had to use its non-model fallback plan.",
    )
    headline: str = Field(
        default="",
        description="Short human-readable explanation for the final outcome.",
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable contributing factors behind the final outcome.",
    )


class ContextState(BaseModel):
    """Session-wide mutable state shared across agent phases.

    The orchestrator creates one instance per run and passes it through
    every phase so that tools and the inference engine can read / update it.
    """

    goal: str = Field(default="", description="Current review or debug objective")
    constraints: list[str] = Field(
        default_factory=list, description="Active constraints for this run"
    )
    decisions: list[DecisionStep] = Field(
        default_factory=list, description="Decision history"
    )
    current_files: list[str] = Field(
        default_factory=list, description="Files currently under inspection"
    )
    errors: list[ErrorDetail] = Field(
        default_factory=list, description="Errors discovered so far"
    )
    run_diagnostics: RunDiagnostics | None = Field(
        default=None,
        description="Final structured diagnostics for why the run stopped or degraded.",
    )
