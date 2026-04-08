"""Structured output formatting.

Converts raw analysis results into the canonical actionable format used by
both CLI rendering and (future) API responses.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """Issue severity levels."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    STYLE = "style"


class ReviewIssue(BaseModel):
    """A single review finding."""

    severity: Severity
    location: str = Field(..., description="file:line or diff hunk reference")
    evidence: str = Field(..., description="Code snippet or observation")
    suggestion: str = Field(..., description="Recommended fix or action")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Model confidence"
    )


class ReviewReport(BaseModel):
    """Aggregated review output for one run."""

    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")
