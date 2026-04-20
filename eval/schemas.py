"""Schemas for golden fixtures and evaluation reports."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean, pstdev
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
    path: str = Field(
        default="",
        description="Canonical repo-relative path for semantic location matching.",
    )
    line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
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
    event_log_path: str | None = Field(
        default=None,
        description="Absolute path to persisted event log under eval/outputs/event_logs (if available)",
    )
    error: str | None = None
    issue_matches: list[EvalIssueMatch] = Field(default_factory=list)
    raw_output: dict[str, Any] = Field(default_factory=dict)
    placeholder_summary: bool = Field(
        default=False,
        description="True when the pipeline returned a placeholder summary (no submit_review/debug).",
    )
    submit_review_seen_any: bool = Field(default=False)
    submit_debug_seen_any: bool = Field(default=False)
    budget_exhausted: bool = Field(default=False)
    budget_state: str = Field(default="none")
    finish_reasons: list[str] = Field(default_factory=list)


class SampledFixtureResult(BaseModel):
    """K-sample evaluation result for one fixture."""

    fixture_id: str
    fixture_type: FixtureType
    expected_count: int = Field(default=0, ge=0)
    samples: int = Field(default=1, ge=1)
    runs: list[EvalResult] = Field(default_factory=list)
    pass_at_k_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mean_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    hit_rate_stddev: float = Field(default=0.0, ge=0.0)
    mean_false_positive_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    worst_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    best_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    schema_valid_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class MetricSummary(BaseModel):
    """Aggregated metrics for a suite."""

    schema_validity_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    pass_at_k_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mean_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    hit_rate_stddev: float = Field(default=0.0, ge=0.0)
    false_positive_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mean_false_positive_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    sampling_k: int = Field(default=1, ge=1)
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

        valid_count = sum(
            1 for item in results if item.schema_valid and not item.placeholder_summary
        )
        expected_total = sum(item.expected_count for item in results)
        matched_total = sum(item.matched_count for item in results)
        actual_total = sum(item.actual_count for item in results)
        false_positive_total = sum(item.false_positive_count for item in results)

        latencies = [item.latency_seconds for item in results]
        token_values = [float(item.total_tokens) for item in results]

        return cls(
            schema_validity_rate=valid_count / len(results),
            hit_rate=(matched_total / expected_total) if expected_total else 0.0,
            pass_at_k_hit_rate=(matched_total / expected_total) if expected_total else 0.0,
            mean_hit_rate=(matched_total / expected_total) if expected_total else 0.0,
            hit_rate_stddev=0.0,
            false_positive_rate=(
                false_positive_total / actual_total if actual_total else 0.0
            ),
            mean_false_positive_rate=(
                false_positive_total / actual_total if actual_total else 0.0
            ),
            sampling_k=1,
            avg_latency_seconds=float(mean(latencies)),
            p50_latency_seconds=cls._percentile(latencies, 0.5),
            p95_latency_seconds=cls._percentile(latencies, 0.95),
            avg_total_tokens=float(mean(token_values)),
            p50_total_tokens=cls._percentile(token_values, 0.5),
            p95_total_tokens=cls._percentile(token_values, 0.95),
        )

    @classmethod
    def from_sampled_results(cls, sampled_results: list[SampledFixtureResult]) -> "MetricSummary":
        if not sampled_results:
            return cls()

        positive_results = [
            item for item in sampled_results if _sampled_expected_count(item) > 0
        ]
        pass_at_k_values = [item.pass_at_k_hit_rate for item in positive_results]
        mean_hit_values = [item.mean_hit_rate for item in positive_results]
        mean_fp_values = [item.mean_false_positive_rate for item in sampled_results]
        schema_valid_values = [item.schema_valid_rate for item in sampled_results]
        all_runs = [run for item in sampled_results for run in item.runs]

        latencies = [run.latency_seconds for run in all_runs]
        token_values = [float(run.total_tokens) for run in all_runs]

        return cls(
            schema_validity_rate=float(mean(schema_valid_values)),
            hit_rate=float(mean(mean_hit_values)) if mean_hit_values else 0.0,
            pass_at_k_hit_rate=float(mean(pass_at_k_values)) if pass_at_k_values else 0.0,
            mean_hit_rate=float(mean(mean_hit_values)) if mean_hit_values else 0.0,
            hit_rate_stddev=float(pstdev(mean_hit_values)) if len(mean_hit_values) > 1 else 0.0,
            false_positive_rate=float(mean(mean_fp_values)),
            mean_false_positive_rate=float(mean(mean_fp_values)),
            sampling_k=max(item.samples for item in sampled_results),
            avg_latency_seconds=float(mean(latencies)) if latencies else 0.0,
            p50_latency_seconds=cls._percentile(latencies, 0.5),
            p95_latency_seconds=cls._percentile(latencies, 0.95),
            avg_total_tokens=float(mean(token_values)) if token_values else 0.0,
            p50_total_tokens=cls._percentile(token_values, 0.5),
            p95_total_tokens=cls._percentile(token_values, 0.95),
        )


def _sampled_expected_count(item: SampledFixtureResult) -> int:
    if item.expected_count > 0:
        return item.expected_count
    return max((run.expected_count for run in item.runs), default=0)


class EvalReport(BaseModel):
    """Suite-level report."""

    suite: str = Field(default="golden")
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    fixture_count: int = Field(default=0, ge=0)
    metrics: MetricSummary = Field(default_factory=MetricSummary)
    results: list[EvalResult] = Field(default_factory=list)
    sampled_results: list[SampledFixtureResult] = Field(default_factory=list)


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

