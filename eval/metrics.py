"""Metric computation for evaluation results."""

from __future__ import annotations

from pathlib import Path

from eval.schemas import EvalReport, EvalResult, MetricSummary, SampledFixtureResult


def build_metric_summary(
    results: list[EvalResult],
    *,
    sampled_results: list[SampledFixtureResult] | None = None,
) -> MetricSummary:
    """Compute all required metrics from per-fixture results."""
    if sampled_results:
        return MetricSummary.from_sampled_results(sampled_results)
    return MetricSummary.from_results(results)


def build_eval_report(
    suite: str,
    results: list[EvalResult],
    *,
    sampled_results: list[SampledFixtureResult] | None = None,
) -> EvalReport:
    """Build suite-level report with aggregated metrics."""
    sampled = sampled_results or []
    metrics = build_metric_summary(results, sampled_results=sampled)
    return EvalReport(
        suite=suite,
        fixture_count=len(results),
        metrics=metrics,
        results=results,
        sampled_results=sampled,
    )


def write_human_review_template(
    report: EvalReport,
    output_path: str | Path,
) -> Path:
    """Generate human acceptability scoring template."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Human Acceptability Review Sheet",
        "",
        "请为每条样本填写 score(0-5) 与 comment。",
        "",
        "| fixture_id | schema_valid | matched/expected | false_positive | pass@k | mean_hit | stddev | score(0-5) | comment |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    sampled_by_fixture = {item.fixture_id: item for item in report.sampled_results}
    for item in report.results:
        ratio = f"{item.matched_count}/{item.expected_count}"
        sampled = sampled_by_fixture.get(item.fixture_id)
        pass_at_k = f"{sampled.pass_at_k_hit_rate:.2%}" if sampled else "-"
        mean_hit = f"{sampled.mean_hit_rate:.2%}" if sampled else "-"
        stddev = f"{sampled.hit_rate_stddev:.2%}" if sampled else "-"
        lines.append(
            "| "
            f"{item.fixture_id} | {item.schema_valid} | {ratio} | "
            f"{item.false_positive_count} | {pass_at_k} | {mean_hit} | {stddev} |  |  |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

