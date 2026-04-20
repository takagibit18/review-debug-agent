"""Tests for eval gate CLI thresholds."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from eval.gate import main


def test_eval_gate_passes_when_metrics_within_threshold(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.9,"false_positive_rate":0.2}}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["--report", str(report)])
    assert result.exit_code == 0
    assert "passed" in result.output.lower()


def test_eval_gate_fails_when_hit_rate_too_low(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.7,"false_positive_rate":0.2}}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["--report", str(report)])
    assert result.exit_code != 0
    assert "eval gate failed" in result.output.lower()
