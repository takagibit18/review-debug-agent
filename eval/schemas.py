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
    location_pattern: str = Field(
        default="", description="Loose pattern matched in issue location."
    )
    path: str = Field(
        default="",
        description="Canonical repo-relative path for semantic location matching.",
    )
    line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    category: str = Field(default="logic")
    description: str = Field(default="")
    root_cause_id: str = Field(
        default="",
        description="Optional annotation grouping symptoms into one expected root cause.",
    )
    repair_unit: str = Field(
        default="",
        description="Optional normalized minimal-repair annotation.",
    )


class ExpectedResult(BaseModel):
    """Expected results against which model output is evaluated."""

    issues: list[ExpectedIssue] = Field(default_factory=list)
    min_issues: int = Field(default=0, ge=0)
    max_issues: int | None = Field(default=None, ge=0)
    is_empty_annotation: bool = Field(default=False)


class FixtureWorkspace(BaseModel):
    """Workspace restoration metadata for a review fixture."""

    kind: Literal["git"] = Field(default="git")
    repo_url: str = Field(..., min_length=1)
    base_sha: str = Field(default="")
    head_sha: str = Field(default="")
    checkout_sha: str = Field(..., min_length=1)
    diff_base_sha: str = Field(default="")


class FixtureInput(BaseModel):
    """Input payload used by runner."""

    diff_text: str = Field(default="")
    files: dict[str, str] = Field(default_factory=dict)
    error_log: str | None = Field(default=None)
    workspace: FixtureWorkspace | None = Field(default=None)


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


class ReviewProcessMetrics(BaseModel):
    """Process-level evidence collected from one review run timeline."""

    model_raw_issue_count: int = Field(default=0, ge=0)
    verifier_candidate_count: int = Field(default=0, ge=0)
    candidate_issue_count: int = Field(default=0, ge=0)
    evidence_bound_issue_count: int = Field(default=0, ge=0)
    verifier_accepted_count: int = Field(default=0, ge=0)
    verifier_rejected_count: int = Field(default=0, ge=0)
    verifier_needs_evidence_count: int = Field(default=0, ge=0)
    verifier_downgraded_count: int = Field(default=0, ge=0)
    raw_verifier_accepted_count: int = Field(default=0, ge=0)
    raw_verifier_rejected_count: int = Field(default=0, ge=0)
    raw_verifier_needs_evidence_count: int = Field(default=0, ge=0)
    raw_verifier_downgraded_count: int = Field(default=0, ge=0)
    deterministic_evidence_checked_count: int = Field(default=0, ge=0)
    deterministic_evidence_passed_count: int = Field(default=0, ge=0)
    deterministic_evidence_rejected_count: int = Field(default=0, ge=0)
    first_pass_accept_count: int = Field(default=0, ge=0)
    required_step_count: int = Field(default=0, ge=0)
    completed_required_step_count: int = Field(default=0, ge=0)
    workflow_filtered_issue_count: int = Field(default=0, ge=0)
    final_effective_issue_count: int = Field(default=0, ge=0)
    workflow_invalid: bool = False
    workflow_missing_steps: list[str] = Field(default_factory=list)
    duplicate_tool_call_count: int = Field(default=0, ge=0)
    structured_hypothesis_count: int = Field(default=0, ge=0)
    evidence_complete_count: int = Field(default=0, ge=0)
    candidate_context_tokens: int = Field(default=0, ge=0)
    included_graph_nodes: int = Field(default=0, ge=0)
    included_graph_paths: int = Field(default=0, ge=0)
    discarded_graph_paths: int = Field(default=0, ge=0)
    reviewer_tool_call_count: int = Field(default=0, ge=0)
    unused_context_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    edge_confidence_contribution: float = Field(default=0.0, ge=0.0, le=1.0)
    graph_build_latency_seconds: float = Field(default=0.0, ge=0.0)
    incremental_update_latency_seconds: float = Field(default=0.0, ge=0.0)
    persistent_cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    consolidator_block_count: int = Field(default=0, ge=0)
    consolidator_average_block_size: float = Field(default=0.0, ge=0.0)
    consolidator_proposal_count: int = Field(default=0, ge=0)
    consolidator_accepted_cluster_count: int = Field(default=0, ge=0)
    consolidator_rejected_cluster_count: int = Field(default=0, ge=0)
    final_root_cause_count: int = Field(default=0, ge=0)
    finding_inflation_ratio: float = Field(default=0.0, ge=0.0)
    event_log_status: Literal["ok", "missing", "parse_error"] = "missing"

    @property
    def evidence_binding_rate(self) -> float:
        return (
            self.evidence_bound_issue_count / self.candidate_issue_count
            if self.candidate_issue_count
            else 1.0
        )

    @property
    def required_step_completion_rate(self) -> float:
        return (
            self.completed_required_step_count / self.required_step_count
            if self.required_step_count
            else 1.0
        )

    @property
    def evidence_validation_pass_rate(self) -> float:
        return (
            self.deterministic_evidence_passed_count
            / self.deterministic_evidence_checked_count
            if self.deterministic_evidence_checked_count
            else 1.0
        )


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
    expected_root_cause_count: int = Field(default=0, ge=0)
    matched_root_cause_count: int = Field(default=0, ge=0)
    over_merge_count: int = Field(default=0, ge=0)
    under_merge_count: int = Field(default=0, ge=0)
    repair_unit_expected_count: int = Field(default=0, ge=0)
    repair_unit_matched_count: int = Field(default=0, ge=0)
    evidence_complete_count: int = Field(default=0, ge=0)
    final_finding_count: int = Field(default=0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0.0)
    total_tokens: int = Field(default=0, ge=0)
    event_log_path: str | None = Field(
        default=None,
        description="Absolute path to persisted event log under eval/outputs/event_logs (if available)",
    )
    error: str | None = None
    stage_timings: dict[str, float] = Field(default_factory=dict)
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
    workflow_invalid: bool = Field(default=False)
    workflow_missing_steps: list[str] = Field(default_factory=list)
    process_metrics: ReviewProcessMetrics = Field(default_factory=ReviewProcessMetrics)


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
    root_cause_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    over_merge_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    under_merge_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    repair_unit_accuracy: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    finding_inflation_ratio: float = Field(default=0.0, ge=0.0)
    final_finding_count: int = Field(default=0, ge=0)
    sampling_k: int = Field(default=1, ge=1)
    avg_latency_seconds: float = Field(default=0.0, ge=0.0)
    p50_latency_seconds: float = Field(default=0.0, ge=0.0)
    p95_latency_seconds: float = Field(default=0.0, ge=0.0)
    avg_total_tokens: float = Field(default=0.0, ge=0.0)
    p50_total_tokens: float = Field(default=0.0, ge=0.0)
    p95_total_tokens: float = Field(default=0.0, ge=0.0)
    evidence_binding_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    verifier_accept_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    verifier_reject_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    first_pass_accept_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    required_step_completion_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    duplicate_tool_call_rate: float = Field(default=0.0, ge=0.0)
    cost_per_accepted_finding: float = Field(default=0.0, ge=0.0)
    avg_candidate_context_tokens: float = Field(default=0.0, ge=0.0)
    included_graph_nodes: int = Field(default=0, ge=0)
    included_graph_paths: int = Field(default=0, ge=0)
    discarded_graph_paths: int = Field(default=0, ge=0)
    reviewer_tool_call_count: int = Field(default=0, ge=0)
    unused_context_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    edge_confidence_contribution: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_graph_build_latency_seconds: float = Field(default=0.0, ge=0.0)
    avg_incremental_update_latency_seconds: float = Field(default=0.0, ge=0.0)
    persistent_cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    consolidator_block_count: int = Field(default=0, ge=0)
    average_consolidator_block_size: float = Field(default=0.0, ge=0.0)
    consolidator_proposal_count: int = Field(default=0, ge=0)
    consolidator_accepted_cluster_count: int = Field(default=0, ge=0)
    consolidator_rejected_cluster_count: int = Field(default=0, ge=0)
    model_raw_issue_count: int = Field(default=0, ge=0)
    verifier_candidate_count: int = Field(default=0, ge=0)
    verifier_accepted_count: int = Field(default=0, ge=0)
    verifier_rejected_count: int = Field(default=0, ge=0)
    verifier_needs_evidence_count: int = Field(default=0, ge=0)
    verifier_downgraded_count: int = Field(default=0, ge=0)
    raw_verifier_accepted_count: int = Field(default=0, ge=0)
    raw_verifier_rejected_count: int = Field(default=0, ge=0)
    raw_verifier_needs_evidence_count: int = Field(default=0, ge=0)
    raw_verifier_downgraded_count: int = Field(default=0, ge=0)
    deterministic_evidence_checked_count: int = Field(default=0, ge=0)
    deterministic_evidence_passed_count: int = Field(default=0, ge=0)
    deterministic_evidence_rejected_count: int = Field(default=0, ge=0)
    evidence_validation_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    workflow_filtered_issue_count: int = Field(default=0, ge=0)
    final_effective_issue_count: int = Field(default=0, ge=0)
    workflow_invalid_run_count: int = Field(default=0, ge=0)
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
        process = _aggregate_process_metrics(results)
        quality = _aggregate_quality_metrics(results)

        return cls(
            schema_validity_rate=valid_count / len(results),
            hit_rate=(matched_total / expected_total) if expected_total else 0.0,
            pass_at_k_hit_rate=(matched_total / expected_total)
            if expected_total
            else 0.0,
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
            **quality,
            **process,
        )

    @classmethod
    def from_sampled_results(
        cls, sampled_results: list[SampledFixtureResult]
    ) -> "MetricSummary":
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
        process = _aggregate_process_metrics(all_runs)
        quality = _aggregate_quality_metrics(all_runs)

        return cls(
            schema_validity_rate=float(mean(schema_valid_values)),
            hit_rate=float(mean(mean_hit_values)) if mean_hit_values else 0.0,
            pass_at_k_hit_rate=float(mean(pass_at_k_values))
            if pass_at_k_values
            else 0.0,
            mean_hit_rate=float(mean(mean_hit_values)) if mean_hit_values else 0.0,
            hit_rate_stddev=float(pstdev(mean_hit_values))
            if len(mean_hit_values) > 1
            else 0.0,
            false_positive_rate=float(mean(mean_fp_values)),
            mean_false_positive_rate=float(mean(mean_fp_values)),
            sampling_k=max(item.samples for item in sampled_results),
            avg_latency_seconds=float(mean(latencies)) if latencies else 0.0,
            p50_latency_seconds=cls._percentile(latencies, 0.5),
            p95_latency_seconds=cls._percentile(latencies, 0.95),
            avg_total_tokens=float(mean(token_values)) if token_values else 0.0,
            p50_total_tokens=cls._percentile(token_values, 0.5),
            p95_total_tokens=cls._percentile(token_values, 0.95),
            **quality,
            **process,
        )


def _sampled_expected_count(item: SampledFixtureResult) -> int:
    if item.expected_count > 0:
        return item.expected_count
    return max((run.expected_count for run in item.runs), default=0)


def _aggregate_process_metrics(
    results: list[EvalResult],
) -> dict[str, int | float]:
    raw_issues = sum(item.process_metrics.model_raw_issue_count for item in results)
    verifier_candidates = sum(
        item.process_metrics.verifier_candidate_count for item in results
    )
    candidates = sum(item.process_metrics.candidate_issue_count for item in results)
    evidence_bound = sum(
        item.process_metrics.evidence_bound_issue_count for item in results
    )
    accepted = sum(item.process_metrics.verifier_accepted_count for item in results)
    rejected = sum(item.process_metrics.verifier_rejected_count for item in results)
    first_pass = sum(item.process_metrics.first_pass_accept_count for item in results)
    required = sum(item.process_metrics.required_step_count for item in results)
    completed = sum(
        item.process_metrics.completed_required_step_count for item in results
    )
    needs_evidence = sum(
        item.process_metrics.verifier_needs_evidence_count for item in results
    )
    downgraded = sum(item.process_metrics.verifier_downgraded_count for item in results)
    raw_accepted = sum(
        item.process_metrics.raw_verifier_accepted_count for item in results
    )
    raw_rejected = sum(
        item.process_metrics.raw_verifier_rejected_count for item in results
    )
    raw_needs_evidence = sum(
        item.process_metrics.raw_verifier_needs_evidence_count for item in results
    )
    raw_downgraded = sum(
        item.process_metrics.raw_verifier_downgraded_count for item in results
    )
    deterministic_checked = sum(
        item.process_metrics.deterministic_evidence_checked_count for item in results
    )
    deterministic_passed = sum(
        item.process_metrics.deterministic_evidence_passed_count for item in results
    )
    deterministic_rejected = sum(
        item.process_metrics.deterministic_evidence_rejected_count for item in results
    )
    filtered = sum(
        item.process_metrics.workflow_filtered_issue_count for item in results
    )
    final_effective = sum(
        item.process_metrics.final_effective_issue_count for item in results
    )
    invalid_runs = sum(1 for item in results if item.process_metrics.workflow_invalid)
    duplicates = sum(item.process_metrics.duplicate_tool_call_count for item in results)
    tokens = sum(item.total_tokens for item in results)
    context_tokens = sum(
        item.process_metrics.candidate_context_tokens for item in results
    )
    included_nodes = sum(item.process_metrics.included_graph_nodes for item in results)
    included_paths = sum(item.process_metrics.included_graph_paths for item in results)
    discarded_paths = sum(
        item.process_metrics.discarded_graph_paths for item in results
    )
    reviewer_tools = sum(
        item.process_metrics.reviewer_tool_call_count for item in results
    )
    graph_latencies = [
        item.process_metrics.graph_build_latency_seconds for item in results
    ]
    incremental_latencies = [
        item.process_metrics.incremental_update_latency_seconds for item in results
    ]
    cache_rates = [item.process_metrics.persistent_cache_hit_rate for item in results]
    unused_ratios = [item.process_metrics.unused_context_ratio for item in results]
    edge_contributions = [
        item.process_metrics.edge_confidence_contribution for item in results
    ]
    block_count = sum(item.process_metrics.consolidator_block_count for item in results)
    proposal_count = sum(
        item.process_metrics.consolidator_proposal_count for item in results
    )
    accepted_clusters = sum(
        item.process_metrics.consolidator_accepted_cluster_count for item in results
    )
    rejected_clusters = sum(
        item.process_metrics.consolidator_rejected_cluster_count for item in results
    )
    weighted_block_size = sum(
        item.process_metrics.consolidator_average_block_size
        * item.process_metrics.consolidator_block_count
        for item in results
    )
    return {
        "model_raw_issue_count": raw_issues,
        "verifier_candidate_count": verifier_candidates,
        "verifier_accepted_count": accepted,
        "verifier_rejected_count": rejected,
        "verifier_needs_evidence_count": needs_evidence,
        "verifier_downgraded_count": downgraded,
        "raw_verifier_accepted_count": raw_accepted,
        "raw_verifier_rejected_count": raw_rejected,
        "raw_verifier_needs_evidence_count": raw_needs_evidence,
        "raw_verifier_downgraded_count": raw_downgraded,
        "deterministic_evidence_checked_count": deterministic_checked,
        "deterministic_evidence_passed_count": deterministic_passed,
        "deterministic_evidence_rejected_count": deterministic_rejected,
        "workflow_filtered_issue_count": filtered,
        "final_effective_issue_count": final_effective,
        "workflow_invalid_run_count": invalid_runs,
        "evidence_binding_rate": evidence_bound / candidates if candidates else 1.0,
        "verifier_accept_rate": accepted / candidates if candidates else 0.0,
        "verifier_reject_rate": rejected / candidates if candidates else 0.0,
        "evidence_validation_pass_rate": (
            deterministic_passed / deterministic_checked
            if deterministic_checked
            else 1.0
        ),
        "first_pass_accept_rate": first_pass / accepted if accepted else 0.0,
        "required_step_completion_rate": completed / required if required else 1.0,
        "duplicate_tool_call_rate": duplicates / candidates if candidates else 0.0,
        "cost_per_accepted_finding": tokens / accepted if accepted else 0.0,
        "avg_candidate_context_tokens": context_tokens / len(results)
        if results
        else 0.0,
        "included_graph_nodes": included_nodes,
        "included_graph_paths": included_paths,
        "discarded_graph_paths": discarded_paths,
        "reviewer_tool_call_count": reviewer_tools,
        "unused_context_ratio": float(mean(unused_ratios)) if unused_ratios else 0.0,
        "edge_confidence_contribution": (
            float(mean(edge_contributions)) if edge_contributions else 0.0
        ),
        "avg_graph_build_latency_seconds": (
            float(mean(graph_latencies)) if graph_latencies else 0.0
        ),
        "avg_incremental_update_latency_seconds": (
            float(mean(incremental_latencies)) if incremental_latencies else 0.0
        ),
        "persistent_cache_hit_rate": float(mean(cache_rates)) if cache_rates else 0.0,
        "consolidator_block_count": block_count,
        "average_consolidator_block_size": (
            weighted_block_size / block_count if block_count else 0.0
        ),
        "consolidator_proposal_count": proposal_count,
        "consolidator_accepted_cluster_count": accepted_clusters,
        "consolidator_rejected_cluster_count": rejected_clusters,
    }


def _aggregate_quality_metrics(results: list[EvalResult]) -> dict[str, int | float]:
    expected_roots = sum(item.expected_root_cause_count for item in results)
    matched_roots = sum(item.matched_root_cause_count for item in results)
    over_merges = sum(item.over_merge_count for item in results)
    under_merges = sum(item.under_merge_count for item in results)
    repair_expected = sum(item.repair_unit_expected_count for item in results)
    repair_matched = sum(item.repair_unit_matched_count for item in results)
    evidence_complete = sum(item.evidence_complete_count for item in results)
    final_findings = sum(item.final_finding_count for item in results)
    return {
        "root_cause_coverage": (
            matched_roots / expected_roots if expected_roots else 0.0
        ),
        "over_merge_rate": over_merges / final_findings if final_findings else 0.0,
        "under_merge_rate": under_merges / expected_roots if expected_roots else 0.0,
        "repair_unit_accuracy": (
            repair_matched / repair_expected if repair_expected else 0.0
        ),
        "evidence_completeness": (
            evidence_complete / final_findings if final_findings else 0.0
        ),
        "finding_inflation_ratio": (
            final_findings / expected_roots if expected_roots else 0.0
        ),
        "final_finding_count": final_findings,
    }


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
