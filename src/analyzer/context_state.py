"""Structured state management for review / debug sessions.

Tracks the evolving context — goal, constraints, decisions, files under
inspection, and accumulated errors — so that the agent loop and tools can
make informed decisions at each phase.
"""

from __future__ import annotations

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
