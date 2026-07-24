"""Typed finding-hypothesis and evidence-provenance primitives.

The public :class:`~src.analyzer.output_formatter.ReviewIssue` keeps the v0.2.2
fields for compatibility and embeds these v0.2.3 fields.  Keeping the small
types in this module avoids coupling graph, verifier, and publisher layers.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, model_validator


FINDING_SCHEMA_VERSION = "2.0"
CounterfactualResult = Literal["yes", "no", "uncertain"]
EvidenceEligibility = Literal["strong", "exploratory", "none"]
EvidenceRole = Literal["cause", "contract", "trigger", "impact", "related"]


class SourceAnchor(BaseModel):
    """A repository-relative source location with optional symbol identity."""

    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    symbol_id: str = ""

    @model_validator(mode="after")
    def _ordered_span(self) -> "SourceAnchor":
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError("end_line must be greater than or equal to line")
        self.file = normalize_repo_path(self.file)
        return self

    @property
    def location(self) -> str:
        suffix = str(self.line)
        if self.end_line is not None and self.end_line != self.line:
            suffix += f"-{self.end_line}"
        return f"{self.file}:{suffix}"


class RelatedLocation(SourceAnchor):
    """A secondary location participating in the same independent repair unit."""

    role: EvidenceRole = "related"
    description: str = ""


class RepairIntent(BaseModel):
    """Minimal repair signature proposed by the reviewer."""

    action: str = ""
    targets: list[str] = Field(default_factory=list)
    boundary: str = ""

    def normalized_targets(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    normalize_semantic_token(value)
                    for value in self.targets
                    if normalize_semantic_token(value)
                }
            )
        )


class EvidenceProvenance(BaseModel):
    """One evidence claim bound to context actually sent to a model."""

    candidate_id: str = ""
    context_manifest_id: str = ""
    retrieval_source: str = "reviewer_context"
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    symbol_id: str = ""
    context_hash: str = ""
    edge_kind: str = ""
    edge_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    resolver: str = ""
    evidence_eligibility: EvidenceEligibility = "strong"
    statement: str = ""

    @model_validator(mode="after")
    def _normalize(self) -> "EvidenceProvenance":
        if self.file:
            self.file = normalize_repo_path(self.file)
        if (
            self.line is not None
            and self.end_line is not None
            and self.end_line < self.line
        ):
            raise ValueError("evidence end_line must be >= line")
        return self

    @property
    def location(self) -> str:
        if not self.file or self.line is None:
            return ""
        suffix = str(self.line)
        if self.end_line is not None and self.end_line != self.line:
            suffix += f"-{self.end_line}"
        return f"{self.file}:{suffix}"


def normalize_repo_path(value: str) -> str:
    """Normalize a model-provided repository path without resolving it on disk."""

    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def normalize_semantic_token(value: str) -> str:
    """Stable conservative normalization for repair/invariant comparisons."""

    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def context_hash(content: str) -> str:
    """Return the canonical SHA-256 digest used by context manifests."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
