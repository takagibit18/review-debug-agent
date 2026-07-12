"""Compare an eval candidate report against a frozen baseline."""

from __future__ import annotations

from pathlib import Path

import click
from pydantic import BaseModel, Field

from eval.schemas import EvalReport


class EvalComparison(BaseModel):
    baseline_report: str = ""
    candidate_report: str = ""
    hit_rate_delta: float = 0.0
    false_positive_rate_delta: float = 0.0
    p95_latency_delta_ratio: float = 0.0
    p95_tokens_delta_ratio: float = 0.0
    evidence_binding_rate_delta: float = 0.0
    passed: bool = True
    failures: list[str] = Field(default_factory=list)


def compare_reports(
    baseline: EvalReport,
    candidate: EvalReport,
    *,
    baseline_report: str = "",
    candidate_report: str = "",
) -> EvalComparison:
    """Return deterministic v0.2 quality and cost regression decisions."""
    baseline_metrics = baseline.metrics
    candidate_metrics = candidate.metrics
    hit_delta = _delta(candidate_metrics.hit_rate, baseline_metrics.hit_rate)
    fp_delta = _delta(
        candidate_metrics.false_positive_rate,
        baseline_metrics.false_positive_rate,
    )
    latency_ratio = _ratio_delta(
        candidate_metrics.p95_latency_seconds,
        baseline_metrics.p95_latency_seconds,
    )
    token_ratio = _ratio_delta(
        candidate_metrics.p95_total_tokens,
        baseline_metrics.p95_total_tokens,
    )
    evidence_delta = _delta(
        candidate_metrics.evidence_binding_rate,
        baseline_metrics.evidence_binding_rate,
    )
    failures: list[str] = []
    if hit_delta < -0.05:
        failures.append("hit_rate_regression")
    if fp_delta > 0:
        failures.append("false_positive_rate_regression")
    if latency_ratio > 0.60:
        failures.append("p95_latency_regression")
    if token_ratio > 0.50:
        failures.append("p95_tokens_regression")
    return EvalComparison(
        baseline_report=baseline_report,
        candidate_report=candidate_report,
        hit_rate_delta=hit_delta,
        false_positive_rate_delta=fp_delta,
        p95_latency_delta_ratio=latency_ratio,
        p95_tokens_delta_ratio=token_ratio,
        evidence_binding_rate_delta=evidence_delta,
        passed=not failures,
        failures=failures,
    )


def _delta(candidate: float, baseline: float) -> float:
    return round(candidate - baseline, 6)


def _ratio_delta(candidate: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0 if candidate <= 0 else 1.0
    return round((candidate - baseline) / baseline, 6)


@click.command()
@click.option("--baseline", "baseline_path", required=True, type=click.Path(exists=True))
@click.option("--candidate", "candidate_path", required=True, type=click.Path(exists=True))
@click.option("--output-json", "output_path", required=True, type=click.Path())
def main(baseline_path: str, candidate_path: str, output_path: str) -> None:
    baseline = EvalReport.model_validate_json(Path(baseline_path).read_text(encoding="utf-8"))
    candidate = EvalReport.model_validate_json(Path(candidate_path).read_text(encoding="utf-8"))
    comparison = compare_reports(
        baseline,
        candidate,
        baseline_report=baseline_path,
        candidate_report=candidate_path,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(comparison.model_dump_json(indent=2), encoding="utf-8")
    click.echo(f"Eval comparison {'passed' if comparison.passed else 'failed'}: {destination}")


if __name__ == "__main__":
    main()
