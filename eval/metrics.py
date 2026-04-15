"""Metric computation for evaluation results."""

from __future__ import annotations

from pathlib import Path

from eval.schemas import EvalReport, EvalResult, MetricSummary


def build_metric_summary(results: list[EvalResult]) -> MetricSummary:
    """Compute all required metrics from per-fixture results."""
    return MetricSummary.from_results(results)


def build_eval_report(suite: str, results: list[EvalResult]) -> EvalReport:
    """Build suite-level report with aggregated metrics."""
    metrics = build_metric_summary(results)
    return EvalReport(
        suite=suite,
        fixture_count=len(results),
        metrics=metrics,
        results=results,
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
        "| fixture_id | schema_valid | matched/expected | false_positive | score(0-5) | comment |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in report.results:
        ratio = f"{item.matched_count}/{item.expected_count}"
        lines.append(
            "| "
            f"{item.fixture_id} | {item.schema_valid} | {ratio} | "
            f"{item.false_positive_count} |  |  |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

