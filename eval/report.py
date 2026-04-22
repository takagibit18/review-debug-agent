"""Report persistence and terminal rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from eval.schemas import EvalReport


def save_report_json(
    report: EvalReport,
    output_dir: str | Path = Path("eval") / "outputs",
    *,
    output_path: str | Path | None = None,
) -> Path:
    """Save report as JSON file."""
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return target

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    generated_path = target_dir / f"{timestamp}_report.json"
    generated_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return generated_path


def render_report(report: EvalReport, console: Console | None = None) -> None:
    """Render summary and detail tables."""
    ui = console or Console()

    summary = Table(title=f"Eval Summary ({report.suite})")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("Fixtures", str(report.fixture_count))
    summary.add_row("Schema Validity", f"{report.metrics.schema_validity_rate:.2%}")
    summary.add_row("Hit Rate", f"{report.metrics.hit_rate:.2%}")
    summary.add_row("Pass@k Hit Rate", f"{report.metrics.pass_at_k_hit_rate:.2%}")
    summary.add_row("Mean Hit Rate", f"{report.metrics.mean_hit_rate:.2%}")
    summary.add_row("Hit Rate Stddev", f"{report.metrics.hit_rate_stddev:.2%}")
    summary.add_row("False Positive Rate", f"{report.metrics.false_positive_rate:.2%}")
    summary.add_row("Mean False Positive Rate", f"{report.metrics.mean_false_positive_rate:.2%}")
    summary.add_row("Sampling K", str(report.metrics.sampling_k))
    summary.add_row("Avg Latency (s)", f"{report.metrics.avg_latency_seconds:.3f}")
    summary.add_row("P50/P95 Latency (s)", f"{report.metrics.p50_latency_seconds:.3f}/{report.metrics.p95_latency_seconds:.3f}")
    summary.add_row("Avg Tokens", f"{report.metrics.avg_total_tokens:.1f}")
    summary.add_row("P50/P95 Tokens", f"{report.metrics.p50_total_tokens:.1f}/{report.metrics.p95_total_tokens:.1f}")
    ui.print(summary)

    detail = Table(title="Fixture Details")
    detail.add_column("fixture_id")
    detail.add_column("valid")
    detail.add_column("placeholder")
    detail.add_column("budget")
    detail.add_column("matched/expected")
    detail.add_column("false_pos")
    detail.add_column("pass@k")
    detail.add_column("mean_hit")
    detail.add_column("stddev")
    detail.add_column("latency(s)")
    detail.add_column("tokens")
    detail.add_column("error")
    sampled_by_fixture = {item.fixture_id: item for item in report.sampled_results}
    for item in report.results:
        sampled = sampled_by_fixture.get(item.fixture_id)
        pass_at_k = f"{sampled.pass_at_k_hit_rate:.2%}" if sampled else "-"
        mean_hit = f"{sampled.mean_hit_rate:.2%}" if sampled else "-"
        stddev = f"{sampled.hit_rate_stddev:.2%}" if sampled else "-"
        detail.add_row(
            item.fixture_id,
            "yes" if item.schema_valid else "no",
            "yes" if item.placeholder_summary else "no",
            item.budget_state,
            f"{item.matched_count}/{item.expected_count}",
            str(item.false_positive_count),
            pass_at_k,
            mean_hit,
            stddev,
            f"{item.latency_seconds:.3f}",
            str(item.total_tokens),
            item.error or "",
        )
    ui.print(detail)

