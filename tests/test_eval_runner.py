"""Tests for eval runner utilities."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from eval.runner import (
    _aggregate_sampled_result,
    _is_empty_business_output,
    _persist_event_log_to_outputs,
    _semantic_location_matches,
    _resolve_event_log_path,
    _resolve_fixture_paths,
    _sanitize_fixture_id_for_filename,
    load_fixtures,
)
from eval.schemas import EvalResult, Fixture, MetricSummary, SampledFixtureResult
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import ReviewResponse


def test_resolve_event_log_path_returns_existing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".cr-debug-agent/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    log_dir = repo_root / ".cr-debug-agent" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "run-1.jsonl"
    log_path.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    resolved = _resolve_event_log_path(repo_root, "run-1")
    assert resolved == str(log_path)


def test_resolve_event_log_path_returns_none_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".cr-debug-agent/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    resolved = _resolve_event_log_path(repo_root, "missing")
    assert resolved is None


def test_sanitize_fixture_id_for_filename() -> None:
    assert _sanitize_fixture_id_for_filename("a/b\\c") == "a_b_c"


def test_persist_event_log_to_outputs_copies_and_returns_absolute(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "run-1.jsonl"
    src.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    out = _persist_event_log_to_outputs(src, "golden_fixture", "run-1")
    assert out is not None
    dest = tmp_path / "eval" / "outputs" / "event_logs" / "golden_fixture_run-1.jsonl"
    assert Path(out) == dest.resolve()
    assert dest.read_text(encoding="utf-8") == '{"event_type":"model_call"}\n'


def test_persist_event_log_to_outputs_returns_none_when_missing_src() -> None:
    assert _persist_event_log_to_outputs(None, "f", "r") is None


def test_persist_event_log_to_outputs_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    assert _persist_event_log_to_outputs(tmp_path / "nope.jsonl", "f", "r") is None


def test_resolve_fixture_paths_prefers_manifest(tmp_path: Path) -> None:
    fixtures_root = tmp_path / "eval" / "fixtures"
    fixtures_root.mkdir(parents=True)
    fixture_path = fixtures_root / "sample.json"
    fixture_path.write_text(
        '{"id":"x","type":"review","source":{"repo_full_name":"a/b","pr_number":1},'
        '"input":{"diff_text":"","files":{}},"expected":{"issues":[]},"metadata":{"reviewed":true}}',
        encoding="utf-8",
    )
    (fixtures_root / "manifest.json").write_text(
        '{"generated_at":"2026-01-01T00:00:00+00:00","entries":['
        '{"fixture_id":"x","fixture_type":"review","repo_full_name":"a/b","pr_number":1,'
        '"suite":"golden","path":"eval/fixtures/sample.json","reviewed":true}]}',
        encoding="utf-8",
    )
    resolved = _resolve_fixture_paths(fixtures_root)
    assert resolved == [fixture_path.resolve()]
    fixtures = load_fixtures(fixtures_root, reviewed_only=True)
    assert len(fixtures) == 1


def test_semantic_location_matches_with_overlap() -> None:
    class _Expected:
        path = "src/main.py"
        line = 10
        end_line = 12
        location_pattern = ""

    assert _semantic_location_matches(_Expected(), "src/main.py:11-13")


def test_sampled_metrics_exclude_empty_fixtures_from_hit_rate() -> None:
    positive = SampledFixtureResult(
        fixture_id="positive",
        fixture_type="review",
        expected_count=1,
        runs=[EvalResult(fixture_id="positive", fixture_type="review", expected_count=1)],
        pass_at_k_hit_rate=0.0,
        mean_hit_rate=0.0,
        schema_valid_rate=1.0,
    )
    empty = SampledFixtureResult(
        fixture_id="empty",
        fixture_type="review",
        expected_count=0,
        runs=[
            EvalResult(
                fixture_id="empty",
                fixture_type="review",
                actual_count=1,
                false_positive_count=1,
            )
        ],
        pass_at_k_hit_rate=1.0,
        mean_hit_rate=1.0,
        mean_false_positive_rate=1.0,
        schema_valid_rate=1.0,
    )

    metrics = MetricSummary.from_sampled_results([positive, empty])

    assert metrics.hit_rate == 0.0
    assert metrics.mean_hit_rate == 0.0
    assert metrics.pass_at_k_hit_rate == 0.0
    assert metrics.false_positive_rate == 0.5


def test_aggregate_sampled_result_preserves_pass_at_k_for_positive_fixture() -> None:
    fixture = Fixture.model_validate(
        {
            "id": "fixture",
            "type": "review",
            "source": {"repo_full_name": "a/b", "pr_number": 1},
            "input": {"diff_text": "", "files": {}},
            "expected": {"issues": [{"location_pattern": "src/main.py"}]},
        }
    )
    runs = [
        EvalResult(
            fixture_id="fixture",
            fixture_type="review",
            expected_count=1,
            matched_count=0,
        ),
        EvalResult(
            fixture_id="fixture",
            fixture_type="review",
            expected_count=1,
            matched_count=1,
        ),
    ]

    sampled = _aggregate_sampled_result(fixture, runs)

    assert sampled.expected_count == 1
    assert sampled.pass_at_k_hit_rate == 1.0
    assert sampled.mean_hit_rate == 0.5


def test_empty_review_output_is_invalid_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(summary="", issues=[]),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is True


def test_placeholder_review_output_is_not_empty_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(
            summary="Review pipeline completed with placeholder summary.",
            issues=[],
        ),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is False


def test_nonempty_no_issue_review_output_is_valid_business_output() -> None:
    response = ReviewResponse(
        run_id="run-1",
        report=ReviewReport(summary="No issues found.", issues=[]),
        context=ContextState(),
    )

    assert _is_empty_business_output(response) is False


def test_golden_fixture_distribution_has_required_buckets() -> None:
    real = load_fixtures(Path("eval") / "fixtures", suite="golden", reviewed_only=True)
    synth = load_fixtures(Path("eval") / "fixtures", suite="golden_synth", reviewed_only=True)

    assert len(real) >= 2
    assert len(synth) >= 8

    tags: Counter[str] = Counter()
    for fixture in synth:
        current = set(fixture.metadata.tags)
        if "should-detect" in current:
            tags["detect"] += 1
        elif "zero-issue" in current:
            tags["zero"] += 1
        elif "boundary-noise" in current:
            tags["boundary"] += 1

    assert tags["detect"] >= 3
    assert tags["zero"] >= 2
    assert tags["boundary"] >= 3
