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


def test_eval_gate_accepts_zero_false_positive_rate(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.0,"false_positive_rate":0.0}}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        [
            "--report",
            str(report),
            "--schema-validity-min",
            "1.0",
            "--hit-rate-min",
            "0.0",
            "--false-positive-rate-max",
            "0.5",
        ],
    )
    assert result.exit_code == 0
    assert "passed" in result.output.lower()


def test_eval_gate_accepts_mvp_plus_hit_rate_threshold(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.6,"false_positive_rate":0.5}}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        [
            "--report",
            str(report),
            "--schema-validity-min",
            "1.0",
            "--hit-rate-min",
            "0.6",
            "--false-positive-rate-max",
            "0.5",
        ],
    )
    assert result.exit_code == 0
    assert "passed" in result.output.lower()


def test_eval_gate_rejects_below_mvp_plus_hit_rate_threshold(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.59,"false_positive_rate":0.2}}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        [
            "--report",
            str(report),
            "--schema-validity-min",
            "1.0",
            "--hit-rate-min",
            "0.6",
            "--false-positive-rate-max",
            "0.5",
        ],
    )
    assert result.exit_code != 0
    assert "hit_rate=0.590 < 0.600" in result.output


def test_eval_gate_rejects_failed_baseline_comparison(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    comparison = tmp_path / "comparison.json"
    report.write_text(
        '{"metrics":{"schema_validity_rate":1.0,"hit_rate":0.8,"false_positive_rate":0.1}}',
        encoding="utf-8",
    )
    comparison.write_text(
        '{"passed":false,"failures":["hit_rate_regression","p95_tokens_regression"]}',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "--report",
            str(report),
            "--comparison",
            str(comparison),
            "--hit-rate-min",
            "0.6",
        ],
    )

    assert result.exit_code != 0
    assert "baseline comparison failed" in result.output.lower()
    assert "hit_rate_regression" in result.output
