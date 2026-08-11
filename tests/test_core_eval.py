"""Core Eval v1 contracts and deterministic judge tests."""

from __future__ import annotations

from pathlib import Path

from eval.core_eval import (
    CoreEvalReport,
    CoreFixtureSpec,
    CoreRunRecord,
    GoldFinding,
    GoldLocation,
    assess_run,
    build_core_report_from_runs,
    load_core_config,
    load_core_fixtures,
    match_review_findings,
    render_core_report,
)
from eval.schemas import EvalResult, ReviewProcessMetrics
from src.analyzer.finding_funnel import FindingFunnel

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "eval/core_eval_v1.yaml"


def _gold(
    finding_id: str = "safe-wrapper-peer-unwrapping",
    *,
    description: str = (
        "SafeHashWrapper equality compares the wrapped value to the peer wrapper "
        "instead of unwrapping other.obj, so equal parameters fail fixture grouping."
    ),
) -> GoldFinding:
    return GoldFinding(
        id=finding_id,
        category="logic",
        severity="warning",
        file="src/_pytest/fixtures.py",
        location=GoldLocation(start_line=244, end_line=250, tolerance_lines=5),
        description=description,
        root_cause="The equality implementation does not unwrap the peer wrapper.",
    )


def _issue(
    *,
    location: str = "src/_pytest/fixtures.py:246",
    mechanism: str = (
        "SafeHashWrapper.__eq__ compares self.obj with the wrapper rather than "
        "unwrapping other.obj, making equal parameter values compare unequal."
    ),
    root_cause_id: str = "",
    confidence: float = 0.9,
    suggestion: str = "Compare self.obj with other.obj after checking the peer type.",
) -> dict[str, object]:
    return {
        "severity": "warning",
        "location": location,
        "evidence": mechanism,
        "suggestion": suggestion,
        "confidence": confidence,
        "root_cause_id": root_cause_id,
        "causal_mechanism": mechanism,
    }


def _raw(*issues: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": "run",
        "report": {"summary": "reviewed", "issues": list(issues)},
    }


def _result(
    *,
    schema_valid: bool = True,
    run_id: str = "run",
    submitted: bool = True,
    placeholder: bool = False,
    error: str | None = None,
    raw_output: dict[str, object] | None = None,
    prepared: bool = True,
    workflow_invalid: bool = False,
) -> EvalResult:
    return EvalResult(
        fixture_id="fixture",
        fixture_type="review",
        run_id=run_id,
        schema_valid=schema_valid,
        submit_review_seen_any=submitted,
        placeholder_summary=placeholder,
        error=error,
        raw_output=raw_output or _raw(),
        stage_timings={"prepare_workspace_seconds": 0.1} if prepared else {},
        workflow_invalid=workflow_invalid,
    )


def test_core_config_is_small_full_workspace_set_with_structured_gold() -> None:
    config = load_core_config(CONFIG_PATH)
    loaded = load_core_fixtures(config, repo_root=ROOT)

    assert len(loaded) == 5
    assert config.runtime.model_max_tokens == 4096
    assert config.runtime.prompt_input_token_budget == 12000
    assert config.runtime.token_budget == 60000
    assert config.runtime.token_hard_budget == 80000
    assert config.runtime.final_submit_reserve_tokens == 12000
    assert config.runtime.final_submit_prompt_token_budget == 4000
    assert config.runtime.final_submit_feedback_token_budget == 1200
    assert sum(spec.role == "candidate" for spec, _ in loaded) == 2
    assert {
        "golden_pydantic_pydantic_pr12568",
        "golden_pydantic_pydantic_pr12590",
        "golden_pytest-dev_pytest_pr13969",
    }.issubset({spec.fixture_id for spec, _ in loaded})
    for spec, fixture in loaded:
        assert fixture.input.workspace is not None
        assert fixture.input.workspace.review_scope == "full_pr"
        assert fixture.input.workspace.checkout_sha == fixture.input.workspace.head_sha
        assert fixture.input.workspace.apply_fixture_diff is False
        assert fixture.input.files == {}
        for gold in spec.gold_findings:
            assert gold.category
            assert gold.severity.value
            assert gold.file
            assert gold.location.start_line <= gold.location.end_line
            assert gold.description
            assert gold.root_cause


def test_semantic_match_uses_underlying_issue_not_exact_wording() -> None:
    quality = match_review_findings([_gold()], _raw(_issue()))

    assert quality.matched_count == 1
    assert quality.false_finding_count == 0
    assert quality.matches[0].gold_id == "safe-wrapper-peer-unwrapping"


def test_one_generated_finding_cannot_match_two_gold_findings() -> None:
    quality = match_review_findings(
        [
            _gold("first-root"),
            _gold("second-root", description="Peer wrappers must unwrap other.obj."),
        ],
        _raw(_issue()),
    )

    assert quality.matched_count == 1
    assert len(quality.missed_gold_ids) == 1
    assert quality.generated_count == 1


def test_obvious_generated_duplicates_are_deduplicated_before_precision() -> None:
    quality = match_review_findings(
        [_gold()],
        _raw(
            _issue(root_cause_id="shared-root", confidence=0.8),
            _issue(root_cause_id="shared-root", confidence=0.95),
        ),
    )

    assert quality.generated_count == 1
    assert quality.duplicate_generated_count == 1
    assert quality.matched_count == 1
    assert quality.false_finding_count == 0


def test_same_location_with_unrelated_root_cause_is_false_finding() -> None:
    quality = match_review_findings(
        [_gold()],
        _raw(
            _issue(
                mechanism=(
                    "The import cache can leak file descriptors during teardown and "
                    "eventually exhaust the process limit."
                ),
                suggestion="Close imported resources when the teardown hook completes.",
            )
        ),
    )

    assert quality.matched_count == 0
    assert quality.false_finding_count == 1


def test_runtime_failures_are_separate_from_quality_misses() -> None:
    spec = CoreFixtureSpec(
        fixture_id="fixture",
        path="fixture.json",
        role="candidate",
        gold_findings=[_gold()],
    )
    config = load_core_config(CONFIG_PATH)
    variant = config.variants[0]

    workspace = assess_run(
        spec,
        variant,
        _result(prepared=False, run_id="", submitted=False, error="checkout failed"),
        attempt=1,
    )
    fixture_validation = assess_run(
        spec,
        variant,
        _result(
            run_id="",
            submitted=False,
            error="diff added line does not match workspace",
        ),
        attempt=1,
    )
    incomplete = assess_run(
        spec,
        variant,
        _result(run_id="", submitted=False, placeholder=True),
        attempt=1,
    )
    validator = assess_run(
        spec,
        variant,
        _result(schema_valid=False, workflow_invalid=True),
        attempt=1,
    )
    quality_miss = assess_run(
        spec,
        variant,
        _result(raw_output=_raw()),
        attempt=1,
    )

    assert workspace.runtime_status == "workspace_failure"
    assert fixture_validation.runtime_status == "fixture_validation_failure"
    assert incomplete.runtime_status == "placeholder_or_incomplete"
    assert validator.runtime_status == "validator_failure"
    assert quality_miss.runtime_status == "valid"
    assert quality_miss.quality is not None
    assert quality_miss.quality.matched_count == 0


def test_report_aggregates_quality_on_valid_runs_and_reliability_on_all_attempts() -> (
    None
):
    config = load_core_config(CONFIG_PATH)
    candidate_spec = config.fixtures[1]
    control_spec = config.fixtures[2]
    baseline = next(item for item in config.variants if item.label == "baseline")
    mergewarden = next(item for item in config.variants if item.label == "mergewarden")
    matching_issue = _issue(root_cause_id=candidate_spec.gold_findings[0].id)
    valid_baseline = assess_run(
        candidate_spec,
        baseline,
        _result(raw_output=_raw(matching_issue)),
        attempt=1,
    )
    valid_mergewarden = assess_run(
        candidate_spec,
        mergewarden,
        _result(raw_output=_raw(matching_issue)),
        attempt=1,
    )
    valid_control = assess_run(
        control_spec,
        baseline,
        _result(raw_output=_raw(_issue())),
        attempt=1,
    )
    incomplete_mergewarden = assess_run(
        control_spec,
        mergewarden,
        _result(run_id="", submitted=False, placeholder=True),
        attempt=1,
    )
    runs: list[CoreRunRecord] = [
        valid_baseline,
        valid_mergewarden,
        valid_control,
        incomplete_mergewarden,
    ]

    report = build_core_report_from_runs(config, runs)
    metrics = {item.label: item for item in report.variants}

    assert metrics["baseline"].quality.evaluated_runs == 2
    assert metrics["baseline"].quality.false_findings == 1
    assert metrics["mergewarden"].quality.evaluated_runs == 1
    assert metrics["mergewarden"].reliability.total_attempts == 2
    assert metrics["mergewarden"].reliability.placeholder_incomplete_runs == 1
    assert metrics["mergewarden"].reliability.valid_completion_rate == 0.5

    markdown = render_core_report(report)
    assert "## Review Quality" in markdown
    assert "## Reliability" in markdown
    assert "## Per-fixture comparison" in markdown
    assert "## Main failure mode" in markdown
    assert "## Deliberately deferred" in markdown
    assert "不主张统计代表性" in markdown


def test_precision_is_zero_when_positive_gold_has_no_generated_findings() -> None:
    config = load_core_config(CONFIG_PATH)
    candidate_spec = config.fixtures[0]
    records = [
        assess_run(
            candidate_spec,
            variant,
            _result(raw_output=_raw()),
            attempt=1,
        )
        for variant in config.variants
    ]

    report = build_core_report_from_runs(config, records)

    assert {item.quality.precision for item in report.variants} == {0.0}
    assert {item.quality.recall for item in report.variants} == {0.0}
    assert {item.quality.f1 for item in report.variants} == {0.0}


def test_report_prioritizes_missing_candidate_completions_over_quality_filters() -> (
    None
):
    config = load_core_config(CONFIG_PATH)
    candidate_spec = config.fixtures[0]
    baseline = next(item for item in config.variants if item.label == "baseline")
    mergewarden = next(item for item in config.variants if item.label == "mergewarden")
    valid_baseline = assess_run(
        candidate_spec,
        baseline,
        _result(raw_output=_raw()),
        attempt=1,
    )
    incomplete_result = _result(
        run_id="",
        submitted=False,
        placeholder=True,
    ).model_copy(
        update={
            "finish_reasons": ["budget_hard_capped"],
            "budget_exhausted": True,
            "budget_state": "hard_capped",
        }
    )
    incomplete_mergewarden = assess_run(
        candidate_spec,
        mergewarden,
        incomplete_result,
        attempt=1,
    )

    markdown = render_core_report(
        build_core_report_from_runs(
            config,
            [valid_baseline, incomplete_mergewarden],
        )
    )

    assert "1/2 个 candidate attempts 未合法完成" in markdown
    assert "mergewarden 1 个 candidate fixtures 缺少 valid completion" in markdown
    assert "runtime reliability 与完整 PR 上下文的预算伸缩" in markdown
    assert "review quality A/B 暂不可比较" in markdown


def test_core_report_aggregates_candidate_funnel_by_variant() -> None:
    config = load_core_config(CONFIG_PATH)
    candidate_spec = config.fixtures[0]
    baseline = next(item for item in config.variants if item.label == "baseline")
    mergewarden = next(item for item in config.variants if item.label == "mergewarden")
    baseline_result = _result(raw_output=_raw()).model_copy(
        update={
            "process_metrics": ReviewProcessMetrics(
                finding_funnel=FindingFunnel(no_finding_run_count=1)
            )
        }
    )
    mergewarden_result = _result(raw_output=_raw(_issue())).model_copy(
        update={
            "process_metrics": ReviewProcessMetrics(
                finding_funnel=FindingFunnel(
                    submitted_finding_count=2,
                    non_risk_not_routed_count=1,
                    severity_calibration_candidate_count=1,
                    calibration_rescue_candidate_count=1,
                    semantic_rejected_count=1,
                    final_risk_finding_count=1,
                )
            )
        }
    )
    report = build_core_report_from_runs(
        config,
        [
            assess_run(candidate_spec, baseline, baseline_result, attempt=1),
            assess_run(candidate_spec, mergewarden, mergewarden_result, attempt=1),
        ],
    )
    by_label = {item.label: item for item in report.variants}

    assert by_label["baseline"].candidate_funnel.no_finding_run_count == 1
    assert by_label["mergewarden"].candidate_funnel.submitted_finding_count == 2
    assert by_label["mergewarden"].candidate_funnel.non_risk_not_routed_count == 1
    assert (
        by_label["mergewarden"].candidate_funnel.calibration_rescue_candidate_count
        == 1
    )
    assert by_label["mergewarden"].candidate_funnel.semantic_rejected_count == 1
    assert by_label["mergewarden"].candidate_funnel.final_risk_finding_count == 1
    markdown = render_core_report(report)
    assert "## Candidate Finding Funnel" in markdown
    assert "| No finding submitted | 1 | 0 |" in markdown
    assert "| Calibration / rescue routed | 0 | 1 |" in markdown
    assert "| Final risk findings | 0 | 1 |" in markdown
    assert "Risk reached final output" in markdown


def test_core_report_deserializes_legacy_payload_without_funnel_fields() -> None:
    config = load_core_config(CONFIG_PATH)
    candidate_spec = config.fixtures[0]
    records = [
        assess_run(
            candidate_spec,
            variant,
            _result(raw_output=_raw()),
            attempt=1,
        )
        for variant in config.variants
    ]
    payload = build_core_report_from_runs(config, records).model_dump()
    for variant in payload["variants"]:
        variant.pop("candidate_funnel", None)
    for run in payload["runs"]:
        run["result"]["process_metrics"].pop("finding_funnel", None)

    restored = CoreEvalReport.model_validate(payload)

    assert all(
        item.candidate_funnel == FindingFunnel() for item in restored.variants
    )
    assert all(
        item.result.process_metrics.finding_funnel == FindingFunnel()
        for item in restored.runs
    )
