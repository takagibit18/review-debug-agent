"""Schemas for golden fixtures and evaluation reports."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.analyzer.output_formatter import Severity

FixtureType = Literal["review", "debug"]


class FixtureSource(BaseModel):
    """Source metadata for one fixture."""

    repo_full_name: str = Field(..., description="owner/repo")
    pr_number: int = Field(..., ge=1)
    url: str = Field(default="")
    merge_commit_sha: str = Field(default="")
    title: str = Field(default="")


class ExpectedIssue(BaseModel):
    """Expected issue annotation for one fixture."""

    severity: Severity = Field(default=Severity.WARNING)
    location_pattern: str = Field(default="", description="Loose pattern matched in issue location.")
    category: str = Field(default="logic")
    description: str = Field(default="")


class ExpectedResult(BaseModel):
    """Expected results against which model output is evaluated."""

    issues: list[ExpectedIssue] = Field(default_factory=list)
    min_issues: int = Field(default=0, ge=0)
    max_issues: int | None = Field(default=None, ge=0)
    is_empty_annotation: bool = Field(default=False)


class FixtureInput(BaseModel):
    """Input payload used by runner."""

    diff_text: str = Field(default="")
    files: dict[str, str] = Field(default_factory=dict)
    error_log: str | None = Field(default=None)


class FixtureMeta(BaseModel):
    """Auxiliary metadata for filtering and auditing fixtures."""

    suite: str = Field(default="golden")
    tags: list[str] = Field(default_factory=list)
    difficulty: Literal["easy", "medium", "hard"] = Field(default="medium")
    annotated_by: str = Field(default="llm_draft")
    reviewed: bool = Field(default=False)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Fixture(BaseModel):
    """One golden-set fixture."""

    id: str = Field(..., min_length=1)
    type: FixtureType
    source: FixtureSource
    input: FixtureInput
    expected: ExpectedResult = Field(default_factory=ExpectedResult)
    metadata: FixtureMeta = Field(default_factory=FixtureMeta)


class EvalIssueMatch(BaseModel):
    """Matching result for one expected issue."""

    expected_index: int
    matched: bool
    matched_actual_index: int | None = None


class EvalResult(BaseModel):
    """Per-fixture evaluation result."""

    fixture_id: str
    fixture_type: FixtureType
    run_id: str = Field(default="")
    schema_valid: bool = Field(default=False)
    expected_count: int = Field(default=0, ge=0)
    actual_count: int = Field(default=0, ge=0)
    matched_count: int = Field(default=0, ge=0)
    false_positive_count: int = Field(default=0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0.0)
    total_tokens: int = Field(default=0, ge=0)
    error: str | None = None
    issue_matches: list[EvalIssueMatch] = Field(default_factory=list)
    raw_output: dict[str, Any] = Field(default_factory=dict)


class MetricSummary(BaseModel):
    """Aggregated metrics for a suite."""

    schema_validity_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    false_positive_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_latency_seconds: float = Field(default=0.0, ge=0.0)
    p50_latency_seconds: float = Field(default=0.0, ge=0.0)
    p95_latency_seconds: float = Field(default=0.0, ge=0.0)
    avg_total_tokens: float = Field(default=0.0, ge=0.0)
    p50_total_tokens: float = Field(default=0.0, ge=0.0)
    p95_total_tokens: float = Field(default=0.0, ge=0.0)
    human_acceptability_note: str = Field(
        default="Manual review template generated; scores are filled offline."
    )

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        ordered = sorted(values)
        rank = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
        return float(ordered[rank])

    @classmethod
    def from_results(cls, results: list[EvalResult]) -> "MetricSummary":
        if not results:
            return cls()

        valid_count = sum(1 for item in results if item.schema_valid)
        expected_total = sum(item.expected_count for item in results)
        matched_total = sum(item.matched_count for item in results)
        actual_total = sum(item.actual_count for item in results)
        false_positive_total = sum(item.false_positive_count for item in results)

        latencies = [item.latency_seconds for item in results]
        token_values = [float(item.total_tokens) for item in results]

        return cls(
            schema_validity_rate=valid_count / len(results),
            hit_rate=(matched_total / expected_total) if expected_total else 0.0,
            false_positive_rate=(
                false_positive_total / actual_total if actual_total else 0.0
            ),
            avg_latency_seconds=float(mean(latencies)),
            p50_latency_seconds=cls._percentile(latencies, 0.5),
            p95_latency_seconds=cls._percentile(latencies, 0.95),
            avg_total_tokens=float(mean(token_values)),
            p50_total_tokens=cls._percentile(token_values, 0.5),
            p95_total_tokens=cls._percentile(token_values, 0.95),
        )


class EvalReport(BaseModel):
    """Suite-level report."""

    suite: str = Field(default="golden")
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    fixture_count: int = Field(default=0, ge=0)
    metrics: MetricSummary = Field(default_factory=MetricSummary)
    results: list[EvalResult] = Field(default_factory=list)


class FixtureManifestEntry(BaseModel):
    """One entry in fixtures manifest."""

    fixture_id: str
    suite: str = Field(default="golden")
    fixture_type: FixtureType
    repo_full_name: str
    pr_number: int = Field(..., ge=1)
    path: str
    reviewed: bool = Field(default=False)


class FixtureManifest(BaseModel):
    """Index file for all generated fixtures."""

    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    entries: list[FixtureManifestEntry] = Field(default_factory=list)

