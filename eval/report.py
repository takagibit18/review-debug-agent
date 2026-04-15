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
) -> Path:
    """Save report as JSON file."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    output_path = target_dir / f"{timestamp}_report.json"
    output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return output_path


def render_report(report: EvalReport, console: Console | None = None) -> None:
    """Render summary and detail tables."""
    ui = console or Console()

    summary = Table(title=f"Eval Summary ({report.suite})")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("Fixtures", str(report.fixture_count))
    summary.add_row("Schema Validity", f"{report.metrics.schema_validity_rate:.2%}")
    summary.add_row("Hit Rate", f"{report.metrics.hit_rate:.2%}")
    summary.add_row("False Positive Rate", f"{report.metrics.false_positive_rate:.2%}")
    summary.add_row("Avg Latency (s)", f"{report.metrics.avg_latency_seconds:.3f}")
    summary.add_row("P50/P95 Latency (s)", f"{report.metrics.p50_latency_seconds:.3f}/{report.metrics.p95_latency_seconds:.3f}")
    summary.add_row("Avg Tokens", f"{report.metrics.avg_total_tokens:.1f}")
    summary.add_row("P50/P95 Tokens", f"{report.metrics.p50_total_tokens:.1f}/{report.metrics.p95_total_tokens:.1f}")
    ui.print(summary)

    detail = Table(title="Fixture Details")
    detail.add_column("fixture_id")
    detail.add_column("valid")
    detail.add_column("matched/expected")
    detail.add_column("false_pos")
    detail.add_column("latency(s)")
    detail.add_column("tokens")
    detail.add_column("error")
    for item in report.results:
        detail.add_row(
            item.fixture_id,
            "yes" if item.schema_valid else "no",
            f"{item.matched_count}/{item.expected_count}",
            str(item.false_positive_count),
            f"{item.latency_seconds:.3f}",
            str(item.total_tokens),
            item.error or "",
        )
    ui.print(detail)

