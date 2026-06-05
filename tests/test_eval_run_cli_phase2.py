"""Tests for Phase 2 readiness eval CLI helpers."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from eval.run import main
from eval.schemas import EvalResult, Fixture, FixtureInput, FixtureSource, FixtureWorkspace


def _minimal_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "suite": "golden",
                "generated_at": "2026-05-18T00:00:00+00:00",
                "fixture_count": 1,
                "metrics": {
                    "schema_validity_rate": 1.0,
                    "hit_rate": 0.0,
                    "false_positive_rate": 0.0,
                    "avg_latency_seconds": 1.0,
                    "avg_total_tokens": 10,
                },
                "results": [
                    {
                        "fixture_id": "fixture-a",
                        "fixture_type": "review",
                        "schema_valid": True,
                        "expected_count": 1,
                        "actual_count": 0,
                        "matched_count": 0,
                        "false_positive_count": 0,
                        "latency_seconds": 1.0,
                        "total_tokens": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_diagnose_command_outputs_json(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    _minimal_report(report)

    result = CliRunner().invoke(main, ["diagnose", "--input", str(report)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["fixtures"][0]["reasons"] == ["event_log_missing", "miss_no_issue"]


def test_summarize_log_command_outputs_missing_status(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["summarize-log", "--input", str(tmp_path / "missing.jsonl")],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["event_log_status"] == "missing"


def test_trend_command_outputs_persistent_misses(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    _minimal_report(report)

    result = CliRunner().invoke(main, ["trend", str(report)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["persistent_misses"] == ["fixture-a"]


def test_eval_command_writes_phase2_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    report_path = tmp_path / "ci_report.json"

    async def _fake_run_suite(*args, **kwargs):  # type: ignore[no-untyped-def]
        from eval.schemas import EvalResult, SampledFixtureResult

        run = EvalResult(
            fixture_id="fixture-a",
            fixture_type="review",
            schema_valid=True,
            expected_count=1,
            actual_count=1,
            matched_count=1,
            latency_seconds=1.0,
            total_tokens=10,
        )
        return [
            SampledFixtureResult(
                fixture_id="fixture-a",
                fixture_type="review",
                expected_count=1,
                runs=[run],
            )
        ]

    monkeypatch.setattr("eval.run.load_fixtures", lambda **kwargs: [object()])
    monkeypatch.setattr("eval.run.run_suite", _fake_run_suite)
    result = CliRunner().invoke(
        main,
        ["eval", "--output-json", str(report_path)],
    )

    assert result.exit_code == 0
    assert (tmp_path / "ci_report_diagnostics.json").exists()
    assert (tmp_path / "ci_report_run_summaries.json").exists()
    assert "Diagnostics saved to:" in result.output


def _fixture(fixture_id: str, repo: str) -> Fixture:
    return Fixture(
        id=fixture_id,
        type="review",
        source=FixtureSource(repo_full_name=repo, pr_number=1),
        input=FixtureInput(
            workspace=FixtureWorkspace(
                repo_url=f"https://github.com/{repo}.git",
                checkout_sha="head",
            )
        ),
    )


def test_eval_command_filters_fixtures_by_repo_and_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_path = tmp_path / "batch_report.json"
    seen_ids: list[str] = []

    async def _fake_run_suite(fixtures, *args, **kwargs):  # type: ignore[no-untyped-def]
        from eval.schemas import SampledFixtureResult

        seen_ids.extend(item.id for item in fixtures)
        run = EvalResult(
            fixture_id=fixtures[0].id,
            fixture_type="review",
            schema_valid=True,
        )
        return [
            SampledFixtureResult(
                fixture_id=fixtures[0].id,
                fixture_type="review",
                runs=[run],
            )
        ]

    monkeypatch.setattr(
        "eval.run.load_fixtures",
        lambda **kwargs: [
            _fixture("pytest-a", "pytest-dev/pytest"),
            _fixture("pytest-b", "pytest-dev/pytest"),
            _fixture("pydantic-a", "pydantic/pydantic"),
        ],
    )
    monkeypatch.setattr("eval.run.run_suite", _fake_run_suite)

    result = CliRunner().invoke(
        main,
        [
            "eval",
            "--repo",
            "pytest-dev/pytest",
            "--fixture-id",
            "pytest-b",
            "--output-json",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert seen_ids == ["pytest-b"]


def test_merge_reports_combines_batch_results_and_recomputes_metrics(
    tmp_path: Path,
) -> None:
    report_a = tmp_path / "batch-a.json"
    report_b = tmp_path / "batch-b.json"
    output = tmp_path / "merged.json"
    report_a.write_text(
        json.dumps(
            {
                "suite": "golden",
                "fixture_count": 1,
                "metrics": {},
                "results": [
                    {
                        "fixture_id": "positive-a",
                        "fixture_type": "review",
                        "schema_valid": True,
                        "expected_count": 1,
                        "actual_count": 1,
                        "matched_count": 1,
                        "false_positive_count": 0,
                    }
                ],
                "sampled_results": [],
            }
        ),
        encoding="utf-8",
    )
    report_b.write_text(
        json.dumps(
            {
                "suite": "golden",
                "fixture_count": 1,
                "metrics": {},
                "results": [
                    {
                        "fixture_id": "negative-a",
                        "fixture_type": "review",
                        "schema_valid": True,
                        "expected_count": 0,
                        "actual_count": 1,
                        "matched_count": 0,
                        "false_positive_count": 1,
                    }
                ],
                "sampled_results": [],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "merge-reports",
            str(report_a),
            str(report_b),
            "--output-json",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["fixture_count"] == 2
    assert payload["metrics"]["hit_rate"] == 1.0
    assert payload["metrics"]["false_positive_rate"] == 0.5
    assert [item["fixture_id"] for item in payload["results"]] == [
        "positive-a",
        "negative-a",
    ]
