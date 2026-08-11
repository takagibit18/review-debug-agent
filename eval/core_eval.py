"""Small, reproducible A/B evaluation for the current MergeWarden stage."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import click
import yaml
from pydantic import BaseModel, Field, model_validator

from eval.runner import run_single
from eval.schemas import EvalResult, EvalVariant, Fixture
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, Severity
from src.config import get_settings

CoreFixtureRole = Literal["candidate", "clean_control"]
CoreVariantLabel = Literal["baseline", "mergewarden"]
RuntimeStatus = Literal[
    "valid",
    "placeholder_or_incomplete",
    "workspace_failure",
    "fixture_validation_failure",
    "validator_failure",
    "runtime_error",
]

CORE_MATCHER_VERSION = "core-semantic-v1"
_FIXTURE_VALIDATION_MARKERS = (
    "diff added line does not match workspace",
    "expected issue",
    "expected location",
    "does not map to a changed line",
    "fixture review scope contract",
)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "before",
    "by",
    "can",
    "change",
    "changes",
    "for",
    "from",
    "has",
    "have",
    "in",
    "instead",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "so",
    "that",
    "the",
    "their",
    "this",
    "to",
    "when",
    "which",
    "will",
    "with",
}
_TOKEN_ALIASES = {
    "attrs": "attribute",
    "attributes": "attribute",
    "compared": "compare",
    "compares": "compare",
    "comparing": "compare",
    "equality": "equal",
    "keys": "key",
    "parameters": "parameter",
    "reclassified": "reclassify",
    "reclassifies": "reclassify",
    "reclassifying": "reclassify",
    "routed": "route",
    "routing": "route",
    "semantics": "contract",
    "values": "value",
    "wrapped": "wrap",
    "wrappers": "wrapper",
}


class GoldLocation(BaseModel):
    """Permitted source range for one gold finding."""

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    tolerance_lines: int = Field(default=3, ge=0, le=20)

    @model_validator(mode="after")
    def _ordered_range(self) -> GoldLocation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class GoldFinding(BaseModel):
    """Human-reviewed description of one underlying PR issue."""

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    severity: Severity
    file: str = Field(min_length=1)
    location: GoldLocation
    description: str = Field(min_length=1)
    root_cause: str = Field(min_length=1)


class CoreFixtureSpec(BaseModel):
    """Core-set reference plus its v1 gold findings."""

    fixture_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    role: CoreFixtureRole
    gold_findings: list[GoldFinding] = Field(default_factory=list)
    pair_id: str | None = None

    @model_validator(mode="after")
    def _role_matches_gold(self) -> CoreFixtureSpec:
        if self.role == "candidate" and not self.gold_findings:
            raise ValueError("candidate fixtures require at least one gold finding")
        if self.role == "clean_control" and self.gold_findings:
            raise ValueError("clean controls must not contain gold findings")
        return self


class CoreVariantSpec(BaseModel):
    """One side of the simple A/B comparison."""

    label: CoreVariantLabel
    id: str = Field(min_length=1)
    context_mode: Literal["agent_search", "graph_hybrid"]
    graph_cache_mode: Literal["disabled", "cold", "warm"]

    def as_eval_variant(self) -> EvalVariant:
        """Convert the config entry to the existing runner contract."""
        return EvalVariant(
            id=self.id,
            context_mode=self.context_mode,
            graph_cache_mode=self.graph_cache_mode,
        )


class CoreRuntimeConfig(BaseModel):
    """Runtime controls shared by both variants."""

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_max_tokens: int = Field(default=4096, ge=2048, le=8192)
    prompt_input_token_budget: int = Field(default=12000, ge=4000, le=32000)
    token_budget: int = Field(default=60000, ge=10000, le=120000)
    token_hard_budget: int = Field(default=80000, ge=10000, le=140000)
    final_submit_reserve_tokens: int = Field(default=12000, ge=4000, le=40000)
    final_submit_prompt_token_budget: int = Field(default=4000, ge=1000, le=12000)
    review_max_iterations: int = Field(default=3, ge=2)
    fixture_concurrency: int = Field(default=1, ge=1, le=8)
    repeat_on_instability: bool = True
    max_attempts: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def _ordered_token_budgets(self) -> CoreRuntimeConfig:
        if self.token_hard_budget < self.token_budget:
            raise ValueError("token_hard_budget must be at least token_budget")
        if self.final_submit_reserve_tokens >= self.token_hard_budget:
            raise ValueError(
                "final_submit_reserve_tokens must be below token_hard_budget"
            )
        if self.final_submit_prompt_token_budget > self.final_submit_reserve_tokens:
            raise ValueError(
                "final_submit_prompt_token_budget must not exceed final_submit_reserve_tokens"
            )
        return self


class CoreEvalConfig(BaseModel):
    """Configuration for the deliberately small Core Eval v1."""

    experiment_id: str = Field(default="core-eval-v1", min_length=1)
    description: str = ""
    runtime: CoreRuntimeConfig = Field(default_factory=CoreRuntimeConfig)
    variants: list[CoreVariantSpec]
    fixtures: list[CoreFixtureSpec]

    @model_validator(mode="after")
    def _simple_ab_contract(self) -> CoreEvalConfig:
        labels = [item.label for item in self.variants]
        if sorted(labels) != ["baseline", "mergewarden"]:
            raise ValueError(
                "Core Eval requires exactly baseline and mergewarden variants"
            )
        ids = [item.id for item in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("Core Eval variant ids must be unique")
        baseline = next(item for item in self.variants if item.label == "baseline")
        if (
            baseline.context_mode != "agent_search"
            or baseline.graph_cache_mode != "disabled"
        ):
            raise ValueError(
                "The simple baseline must use agent_search with graph disabled"
            )
        candidate = next(item for item in self.variants if item.label == "mergewarden")
        if candidate.context_mode != "graph_hybrid":
            raise ValueError("The MergeWarden variant must use graph_hybrid")
        if not 5 <= len(self.fixtures) <= 10:
            raise ValueError("Core Eval v1 requires 5-10 curated fixtures")
        fixture_ids = [item.fixture_id for item in self.fixtures]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("Core Eval fixture ids must be unique")
        if sum(item.role == "candidate" for item in self.fixtures) < 2:
            raise ValueError("Core Eval requires at least two issue-bearing candidates")
        return self


class GeneratedFinding(BaseModel):
    """Compact, judge-facing view of one generated actionable finding."""

    actual_index: int = Field(ge=0)
    severity: Severity
    location: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    root_cause_id: str = ""
    text: str


class CoreFindingMatch(BaseModel):
    """One deterministic one-to-one gold/generated match."""

    gold_id: str
    generated_index: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)


class CoreRunQuality(BaseModel):
    """Review-quality counts for one valid completion."""

    gold_count: int = Field(default=0, ge=0)
    generated_count: int = Field(default=0, ge=0)
    matched_count: int = Field(default=0, ge=0)
    false_finding_count: int = Field(default=0, ge=0)
    duplicate_generated_count: int = Field(default=0, ge=0)
    matches: list[CoreFindingMatch] = Field(default_factory=list)
    missed_gold_ids: list[str] = Field(default_factory=list)
    false_generated_indices: list[int] = Field(default_factory=list)


class CoreRunRecord(BaseModel):
    """One measured attempt with reliability and quality kept separate."""

    fixture_id: str
    role: CoreFixtureRole
    variant_label: CoreVariantLabel
    variant_id: str
    attempt: int = Field(ge=1, le=3)
    runtime_status: RuntimeStatus
    valid_completion: bool
    workspace_setup_success: bool
    placeholder_or_incomplete: bool
    workspace_failure: bool
    fixture_validation_failure: bool
    validator_failure: bool
    result: EvalResult
    quality: CoreRunQuality | None = None


class CoreQualityMetrics(BaseModel):
    """Minimal review-quality metrics over valid completions only."""

    evaluated_runs: int = Field(default=0, ge=0)
    gold_findings: int = Field(default=0, ge=0)
    generated_findings: int = Field(default=0, ge=0)
    matched_findings: int = Field(default=0, ge=0)
    false_findings: int = Field(default=0, ge=0)
    precision: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    f1: float | None = Field(default=None, ge=0.0, le=1.0)
    high_severity_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    false_findings_per_pr: float = Field(default=0.0, ge=0.0)


class CoreReliabilityMetrics(BaseModel):
    """Runtime/completion metrics over every measured attempt."""

    total_attempts: int = Field(default=0, ge=0)
    valid_completions: int = Field(default=0, ge=0)
    valid_completion_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    placeholder_incomplete_runs: int = Field(default=0, ge=0)
    workspace_failures: int = Field(default=0, ge=0)
    fixture_validation_failures: int = Field(default=0, ge=0)
    validator_failures: int = Field(default=0, ge=0)
    other_runtime_failures: int = Field(default=0, ge=0)


class CoreVariantMetrics(BaseModel):
    """Quality and reliability for one A/B side."""

    label: CoreVariantLabel
    variant_id: str
    quality: CoreQualityMetrics
    reliability: CoreReliabilityMetrics


class CoreRuntimeContract(BaseModel):
    """Effective shared model and budget settings recorded with the report."""

    model: str
    temperature: float
    model_max_tokens: int = Field(ge=1)
    prompt_input_token_budget: int = Field(ge=1)
    review_max_iterations: int = Field(ge=1)
    agent_max_tool_calls: int = Field(ge=1)
    token_budget: int = Field(ge=1)
    token_hard_budget: int = Field(ge=1)
    final_submit_reserve_tokens: int = Field(default=12000, ge=1)
    final_submit_prompt_token_budget: int = Field(default=4000, ge=1)
    model_request_timeout_seconds: float = Field(gt=0.0)
    agent_tool_timeout_seconds: float = Field(gt=0.0)
    agent_run_timeout_seconds: float = Field(gt=0.0)


class CoreEvalReport(BaseModel):
    """Machine-readable Core Eval v1 result."""

    experiment_id: str
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    matcher_version: str = CORE_MATCHER_VERSION
    set_description: str = "small curated evaluation set"
    config_path: str
    core_fixture_count: int = Field(ge=0)
    candidate_fixture_count: int = Field(ge=0)
    clean_control_count: int = Field(ge=0)
    runtime_contract: CoreRuntimeContract
    fixtures: list[CoreFixtureSpec]
    runs: list[CoreRunRecord]
    variants: list[CoreVariantMetrics]
    readme_conclusion: str


def load_core_config(path: str | Path) -> CoreEvalConfig:
    """Load and validate one Core Eval YAML config."""
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return CoreEvalConfig.model_validate(payload)


def load_core_fixtures(
    config: CoreEvalConfig,
    *,
    repo_root: str | Path,
) -> list[tuple[CoreFixtureSpec, Fixture]]:
    """Load the curated fixtures and enforce full-workspace/gold invariants."""
    root = Path(repo_root).resolve()
    loaded: list[tuple[CoreFixtureSpec, Fixture]] = []
    for spec in config.fixtures:
        fixture_path = (root / spec.path).resolve()
        try:
            fixture_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Fixture path escapes repository root: {spec.path}"
            ) from exc
        fixture = Fixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))
        if fixture.id != spec.fixture_id:
            raise ValueError(
                f"Fixture id mismatch for {spec.path}: {fixture.id} != {spec.fixture_id}"
            )
        if fixture.type != "review":
            raise ValueError(f"Core fixture must be a review fixture: {fixture.id}")
        if not fixture.metadata.reviewed:
            raise ValueError(f"Core fixture must be human reviewed: {fixture.id}")
        if fixture.input.workspace is None:
            raise ValueError(
                f"Core fixture must restore a full git workspace: {fixture.id}"
            )
        workspace = fixture.input.workspace
        if workspace.review_scope == "legacy":
            raise ValueError(
                f"Core fixture must declare full_pr or partial_pr scope: {fixture.id}"
            )
        if workspace.review_scope == "partial_pr":
            for gold in spec.gold_findings:
                gold_path = gold.file.replace("\\", "/")
                if not any(
                    gold_path == path or gold_path.startswith(path.rstrip("/") + "/")
                    for path in workspace.review_paths
                ):
                    raise ValueError(
                        f"Gold finding {gold.id} is outside partial review scope "
                        f"for {fixture.id}"
                    )
        _validate_gold_alignment(spec, fixture)
        loaded.append((spec, fixture))
    return loaded


def _validate_gold_alignment(spec: CoreFixtureSpec, fixture: Fixture) -> None:
    """Keep the v1 gold layer aligned with reviewed legacy annotations."""
    expected = fixture.expected.issues
    if spec.role == "clean_control":
        if expected:
            raise ValueError(f"Clean control has legacy expected issues: {fixture.id}")
        return
    if len(expected) != len(spec.gold_findings):
        raise ValueError(f"Gold finding count drift for {fixture.id}")
    for gold in spec.gold_findings:
        aligned = any(
            item.path.replace("\\", "/") == gold.file.replace("\\", "/")
            and item.line is not None
            and item.line <= gold.location.end_line + gold.location.tolerance_lines
            and (item.end_line or item.line)
            >= gold.location.start_line - gold.location.tolerance_lines
            for item in expected
        )
        if not aligned:
            raise ValueError(f"Gold finding {gold.id} is not aligned with {fixture.id}")


def assess_run(
    spec: CoreFixtureSpec,
    variant: CoreVariantSpec,
    result: EvalResult,
    *,
    attempt: int,
) -> CoreRunRecord:
    """Classify runtime outcome, then judge quality only for valid completions."""
    status = _runtime_status(result)
    valid = status == "valid"
    return CoreRunRecord(
        fixture_id=spec.fixture_id,
        role=spec.role,
        variant_label=variant.label,
        variant_id=variant.id,
        attempt=attempt,
        runtime_status=status,
        valid_completion=valid,
        workspace_setup_success="prepare_workspace_seconds" in result.stage_timings,
        placeholder_or_incomplete=status == "placeholder_or_incomplete",
        workspace_failure=status == "workspace_failure",
        fixture_validation_failure=status == "fixture_validation_failure",
        validator_failure=status == "validator_failure",
        result=result,
        quality=(
            match_review_findings(spec.gold_findings, result.raw_output)
            if valid
            else None
        ),
    )


def _runtime_status(result: EvalResult) -> RuntimeStatus:
    """Return one exclusive runtime state without treating quality misses as invalid."""
    prepared = "prepare_workspace_seconds" in result.stage_timings
    if not prepared and result.error:
        return "workspace_failure"
    error_text = (result.error or "").lower()
    if (
        prepared
        and not result.run_id
        and any(marker in error_text for marker in _FIXTURE_VALIDATION_MARKERS)
    ):
        return "fixture_validation_failure"
    if (
        result.placeholder_summary
        or not result.run_id
        or not result.submit_review_seen_any
    ):
        return "placeholder_or_incomplete" if prepared else "runtime_error"
    if not result.schema_valid or result.workflow_invalid:
        return "validator_failure"
    if result.error:
        return "runtime_error"
    return "valid"


def match_review_findings(
    gold_findings: list[GoldFinding],
    raw_output: dict[str, Any],
) -> CoreRunQuality:
    """Match actionable findings by underlying issue with one-to-one assignment."""
    generated = _extract_generated_findings(raw_output)
    unique, duplicate_count = _deduplicate_generated_findings(generated)
    candidates: list[tuple[float, int, int]] = []
    for gold_index, gold in enumerate(gold_findings):
        for generated_index, finding in enumerate(unique):
            score = _underlying_issue_score(gold, finding)
            if score is not None:
                candidates.append((score, gold_index, generated_index))

    used_gold: set[int] = set()
    used_generated: set[int] = set()
    matches: list[CoreFindingMatch] = []
    for score, gold_index, generated_index in sorted(candidates, reverse=True):
        if gold_index in used_gold or generated_index in used_generated:
            continue
        used_gold.add(gold_index)
        used_generated.add(generated_index)
        matches.append(
            CoreFindingMatch(
                gold_id=gold_findings[gold_index].id,
                generated_index=unique[generated_index].actual_index,
                score=score,
            )
        )

    return CoreRunQuality(
        gold_count=len(gold_findings),
        generated_count=len(unique),
        matched_count=len(matches),
        false_finding_count=max(0, len(unique) - len(matches)),
        duplicate_generated_count=duplicate_count,
        matches=sorted(matches, key=lambda item: item.gold_id),
        missed_gold_ids=[
            item.id
            for index, item in enumerate(gold_findings)
            if index not in used_gold
        ],
        false_generated_indices=[
            item.actual_index
            for index, item in enumerate(unique)
            if index not in used_generated
        ],
    )


def _extract_generated_findings(raw_output: dict[str, Any]) -> list[GeneratedFinding]:
    report = raw_output.get("report", {})
    raw_issues = report.get("issues", []) if isinstance(report, dict) else []
    if not isinstance(raw_issues, list):
        return []
    findings: list[GeneratedFinding] = []
    for index, raw_issue in enumerate(raw_issues):
        try:
            issue = ReviewIssue.model_validate(raw_issue)
        except ValueError:
            continue
        if issue.severity not in {Severity.CRITICAL, Severity.WARNING}:
            continue
        findings.append(
            GeneratedFinding(
                actual_index=index,
                severity=issue.severity,
                location=issue.location,
                confidence=issue.confidence,
                root_cause_id=issue.root_cause_id,
                text=" ".join(
                    value
                    for value in (
                        issue.observed_behavior,
                        issue.causal_mechanism,
                        issue.violated_invariant,
                        issue.trigger,
                        issue.impact,
                        issue.evidence,
                        issue.suggestion,
                    )
                    if value.strip()
                ),
            )
        )
    return findings


def _deduplicate_generated_findings(
    findings: list[GeneratedFinding],
) -> tuple[list[GeneratedFinding], int]:
    unique: list[GeneratedFinding] = []
    for finding in sorted(
        findings,
        key=lambda item: (
            item.severity == Severity.CRITICAL,
            item.confidence,
            -item.actual_index,
        ),
        reverse=True,
    ):
        if any(_generated_findings_are_duplicates(finding, kept) for kept in unique):
            continue
        unique.append(finding)
    unique.sort(key=lambda item: item.actual_index)
    return unique, len(findings) - len(unique)


def _generated_findings_are_duplicates(
    left: GeneratedFinding,
    right: GeneratedFinding,
) -> bool:
    if left.root_cause_id and left.root_cause_id == right.root_cause_id:
        return True
    left_location = normalize_location(left.location)
    right_location = normalize_location(right.location)
    if not left_location.valid or not right_location.valid:
        return False
    if left_location.path != right_location.path:
        return False
    if not _ranges_near(
        left_location.line,
        left_location.end_line,
        right_location.line,
        right_location.end_line,
        tolerance=2,
    ):
        return False
    left_tokens = _semantic_tokens(left.text)
    right_tokens = _semantic_tokens(right.text)
    return _jaccard(left_tokens, right_tokens) >= 0.45


def _underlying_issue_score(
    gold: GoldFinding,
    generated: GeneratedFinding,
) -> float | None:
    parsed = normalize_location(generated.location)
    if not parsed.valid or parsed.path != gold.file.replace("\\", "/"):
        return None
    expected_tokens = _semantic_tokens(
        " ".join(
            (
                gold.id.replace("-", " "),
                gold.category,
                gold.description,
                gold.root_cause,
            )
        )
    )
    actual_tokens = _semantic_tokens(generated.text)
    common_count = len(expected_tokens & actual_tokens)
    coverage = common_count / len(expected_tokens) if expected_tokens else 0.0
    similarity = _jaccard(expected_tokens, actual_tokens)
    exact_root = bool(generated.root_cause_id) and generated.root_cause_id == gold.id

    if parsed.line is None:
        location_score = 0.45
    elif _ranges_near(
        parsed.line,
        parsed.end_line,
        gold.location.start_line,
        gold.location.end_line,
        tolerance=0,
    ):
        location_score = 1.0
    elif _ranges_near(
        parsed.line,
        parsed.end_line,
        gold.location.start_line,
        gold.location.end_line,
        tolerance=gold.location.tolerance_lines,
    ):
        location_score = 0.8
    else:
        location_score = 0.2

    semantic_match = common_count >= 2 and coverage >= 0.16
    if exact_root:
        semantic_match = True
    if not semantic_match:
        return None
    if location_score < 0.8 and not exact_root:
        if parsed.line is not None or common_count < 4 or coverage < 0.28:
            return None
    score = 0.45 * location_score + 0.4 * coverage + 0.15 * similarity
    if exact_root:
        score = max(score, 0.95)
    return min(1.0, round(score, 6))


def _ranges_near(
    left_start: int | None,
    left_end: int | None,
    right_start: int | None,
    right_end: int | None,
    *,
    tolerance: int,
) -> bool:
    if left_start is None or right_start is None:
        return False
    resolved_left_end = left_end or left_start
    resolved_right_end = right_end or right_start
    return (
        left_start <= resolved_right_end + tolerance
        and resolved_left_end + tolerance >= right_start
    )


def _semantic_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", value.lower().replace("_", " ")):
        if raw in _STOP_WORDS or len(raw) < 2:
            continue
        normalized = _TOKEN_ALIASES.get(raw, raw)
        if normalized.endswith("ies") and len(normalized) > 5:
            normalized = f"{normalized[:-3]}y"
        elif normalized.endswith("ing") and len(normalized) > 6:
            normalized = normalized[:-3]
        elif normalized.endswith("ed") and len(normalized) > 5:
            normalized = normalized[:-2]
        elif (
            normalized.endswith("s")
            and len(normalized) > 4
            and not normalized.endswith("ss")
        ):
            normalized = normalized[:-1]
        tokens.add(_TOKEN_ALIASES.get(normalized, normalized))
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


async def run_core_eval(
    config: CoreEvalConfig,
    *,
    config_path: str | Path,
    repo_root: str | Path,
    progress: Callable[[str], None] | None = None,
) -> CoreEvalReport:
    """Run one balanced A/B pass and retry only runtime instability."""
    loaded = load_core_fixtures(config, repo_root=repo_root)
    semaphore = asyncio.Semaphore(config.runtime.fixture_concurrency)

    async def run_pair(
        fixture_index: int,
        spec: CoreFixtureSpec,
        fixture: Fixture,
    ) -> list[CoreRunRecord]:
        async with semaphore:
            records: list[CoreRunRecord] = []
            ordered_variants = (
                config.variants
                if fixture_index % 2 == 0
                else list(reversed(config.variants))
            )
            for variant in ordered_variants:
                for attempt in range(1, config.runtime.max_attempts + 1):
                    if progress is not None:
                        progress(
                            f"START {spec.fixture_id} {variant.id} attempt={attempt}"
                        )
                    result = await run_single(
                        fixture,
                        temperature=config.runtime.temperature,
                        review_max_iterations=config.runtime.review_max_iterations,
                        variant=variant.as_eval_variant(),
                    )
                    record = assess_run(spec, variant, result, attempt=attempt)
                    records.append(record)
                    if progress is not None:
                        progress(
                            f"DONE  {spec.fixture_id} {variant.id} attempt={attempt} "
                            f"status={record.runtime_status}"
                        )
                    if record.valid_completion:
                        break
                    if not config.runtime.repeat_on_instability:
                        break
                    if record.runtime_status not in {
                        "placeholder_or_incomplete",
                        "validator_failure",
                        "runtime_error",
                    }:
                        break
            return records

    env_overrides = {
        "MODEL_MAX_TOKENS": str(config.runtime.model_max_tokens),
        "PROMPT_INPUT_TOKEN_BUDGET": str(config.runtime.prompt_input_token_budget),
        "TOKEN_BUDGET": str(config.runtime.token_budget),
        "TOKEN_HARD_BUDGET": str(config.runtime.token_hard_budget),
        "FINAL_SUBMIT_RESERVE_TOKENS": str(config.runtime.final_submit_reserve_tokens),
        "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET": str(
            config.runtime.final_submit_prompt_token_budget
        ),
        "EVAL_REVIEW_MAX_ITERATIONS_CAP": str(config.runtime.review_max_iterations),
    }
    original_env = {key: os.environ.get(key) for key in env_overrides}
    os.environ.update(env_overrides)
    try:
        nested = await asyncio.gather(
            *(
                run_pair(index, spec, fixture)
                for index, (spec, fixture) in enumerate(loaded)
            )
        )
        records = [record for group in nested for record in group]
        variants = [
            _aggregate_variant_metrics(item, records, config.fixtures)
            for item in config.variants
        ]
        settings = get_settings()
    finally:
        for key, original_value in original_env.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value
    report = CoreEvalReport(
        experiment_id=config.experiment_id,
        config_path=Path(config_path).as_posix(),
        core_fixture_count=len(config.fixtures),
        candidate_fixture_count=sum(
            item.role == "candidate" for item in config.fixtures
        ),
        clean_control_count=sum(
            item.role == "clean_control" for item in config.fixtures
        ),
        runtime_contract=CoreRuntimeContract(
            model=settings.model_name,
            temperature=config.runtime.temperature,
            model_max_tokens=settings.model_max_tokens,
            prompt_input_token_budget=settings.prompt_input_token_budget,
            review_max_iterations=config.runtime.review_max_iterations,
            agent_max_tool_calls=settings.agent_max_tool_calls,
            token_budget=settings.token_budget,
            token_hard_budget=settings.token_hard_budget,
            final_submit_reserve_tokens=settings.final_submit_reserve_tokens,
            final_submit_prompt_token_budget=(
                settings.final_submit_prompt_token_budget
            ),
            model_request_timeout_seconds=settings.model_request_timeout_seconds,
            agent_tool_timeout_seconds=settings.agent_tool_timeout_seconds,
            agent_run_timeout_seconds=settings.agent_run_timeout_seconds,
        ),
        fixtures=config.fixtures,
        runs=records,
        variants=variants,
        readme_conclusion="",
    )
    report.readme_conclusion = _readme_conclusion(report)
    return report


def build_core_report_from_runs(
    config: CoreEvalConfig,
    runs: list[CoreRunRecord],
    *,
    config_path: str = "eval/core_eval_v1.yaml",
) -> CoreEvalReport:
    """Build a report from measured records for tests and offline rerendering."""
    settings = get_settings()
    report = CoreEvalReport(
        experiment_id=config.experiment_id,
        config_path=config_path,
        core_fixture_count=len(config.fixtures),
        candidate_fixture_count=sum(
            item.role == "candidate" for item in config.fixtures
        ),
        clean_control_count=sum(
            item.role == "clean_control" for item in config.fixtures
        ),
        runtime_contract=CoreRuntimeContract(
            model=settings.model_name,
            temperature=config.runtime.temperature,
            model_max_tokens=config.runtime.model_max_tokens,
            prompt_input_token_budget=config.runtime.prompt_input_token_budget,
            review_max_iterations=config.runtime.review_max_iterations,
            agent_max_tool_calls=settings.agent_max_tool_calls,
            token_budget=config.runtime.token_budget,
            token_hard_budget=config.runtime.token_hard_budget,
            final_submit_reserve_tokens=config.runtime.final_submit_reserve_tokens,
            final_submit_prompt_token_budget=(
                config.runtime.final_submit_prompt_token_budget
            ),
            model_request_timeout_seconds=settings.model_request_timeout_seconds,
            agent_tool_timeout_seconds=settings.agent_tool_timeout_seconds,
            agent_run_timeout_seconds=settings.agent_run_timeout_seconds,
        ),
        fixtures=config.fixtures,
        runs=runs,
        variants=[
            _aggregate_variant_metrics(item, runs, config.fixtures)
            for item in config.variants
        ],
        readme_conclusion="",
    )
    report.readme_conclusion = _readme_conclusion(report)
    return report


def _aggregate_variant_metrics(
    variant: CoreVariantSpec,
    records: list[CoreRunRecord],
    fixture_specs: list[CoreFixtureSpec],
) -> CoreVariantMetrics:
    selected = [item for item in records if item.variant_id == variant.id]
    valid = [item for item in selected if item.valid_completion and item.quality]
    gold_total = sum(item.quality.gold_count for item in valid if item.quality)
    generated_total = sum(
        item.quality.generated_count for item in valid if item.quality
    )
    matched_total = sum(item.quality.matched_count for item in valid if item.quality)
    false_total = sum(
        item.quality.false_finding_count for item in valid if item.quality
    )
    precision = (
        matched_total / generated_total
        if generated_total
        else (0.0 if gold_total else None)
    )
    recall = matched_total / gold_total if gold_total else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else (0.0 if precision == 0.0 or recall == 0.0 else None)
    )
    spec_by_id = {item.fixture_id: item for item in fixture_specs}
    critical_gold_total = 0
    critical_matches = 0
    for record in valid:
        quality = record.quality
        if quality is None:
            continue
        spec = spec_by_id[record.fixture_id]
        critical_ids = {
            item.id for item in spec.gold_findings if item.severity == Severity.CRITICAL
        }
        critical_gold_total += len(critical_ids)
        critical_matches += sum(
            match.gold_id in critical_ids for match in quality.matches
        )
    reliability = CoreReliabilityMetrics(
        total_attempts=len(selected),
        valid_completions=len(valid),
        valid_completion_rate=len(valid) / len(selected) if selected else 0.0,
        placeholder_incomplete_runs=sum(
            item.placeholder_or_incomplete for item in selected
        ),
        workspace_failures=sum(item.workspace_failure for item in selected),
        fixture_validation_failures=sum(
            item.fixture_validation_failure for item in selected
        ),
        validator_failures=sum(item.validator_failure for item in selected),
        other_runtime_failures=sum(
            item.runtime_status == "runtime_error" for item in selected
        ),
    )
    return CoreVariantMetrics(
        label=variant.label,
        variant_id=variant.id,
        quality=CoreQualityMetrics(
            evaluated_runs=len(valid),
            gold_findings=gold_total,
            generated_findings=generated_total,
            matched_findings=matched_total,
            false_findings=false_total,
            precision=precision,
            recall=recall,
            f1=f1,
            high_severity_recall=(
                critical_matches / critical_gold_total if critical_gold_total else None
            ),
            false_findings_per_pr=false_total / len(valid) if valid else 0.0,
        ),
        reliability=reliability,
    )


def render_core_report(report: CoreEvalReport) -> str:
    """Render the compact Markdown report requested for Eval v1."""
    by_label = {item.label: item for item in report.variants}
    baseline = by_label["baseline"]
    mergewarden = by_label["mergewarden"]
    successful_setups = sum(item.workspace_setup_success for item in report.runs)
    completion_failures = sum(not item.valid_completion for item in report.runs)
    lines = [
        "# MergeWarden Core Eval v1",
        "",
        (
            f"> {report.set_description}: {report.core_fixture_count} 个 full-workspace PR fixtures "
            f"（{report.candidate_fixture_count} 个 candidate，{report.clean_control_count} 个 clean control）。"
        ),
        "> 该结果用于项目能力验证，不主张统计代表性或显著性。",
        f"> Generated at：`{report.generated_at}`。",
        "> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。",
        "",
        "## Infrastructure",
        "",
        f"- Core fixtures：{report.core_fixture_count}",
        f"- Successful workspace setups：{successful_setups}/{len(report.runs)} attempts",
        f"- Completion failures：{completion_failures}",
        f"- Matcher：`{report.matcher_version}`（deterministic, one-to-one, duplicate-aware）",
        (
            f"- Shared runtime：`{report.runtime_contract.model}`，temperature "
            f"{report.runtime_contract.temperature:g}，{report.runtime_contract.model_max_tokens} output tokens，"
            f"{report.runtime_contract.prompt_input_token_budget} prompt-context tokens，"
            f"{report.runtime_contract.review_max_iterations} iterations，"
            f"{report.runtime_contract.agent_max_tool_calls} tool calls，"
            f"{report.runtime_contract.token_budget}/{report.runtime_contract.token_hard_budget} token budget，"
            f"{report.runtime_contract.final_submit_reserve_tokens} final-submit reserve，"
            f"{report.runtime_contract.final_submit_prompt_token_budget} finalize prompt-context tokens"
        ),
        "",
        "## Review Quality",
        "",
        "Quality 仅统计 valid completions。",
        "",
        "| Metric | Baseline | MergeWarden |",
        "|---|---:|---:|",
        f"| Precision | {_percent(baseline.quality.precision)} | {_percent(mergewarden.quality.precision)} |",
        f"| Recall | {_percent(baseline.quality.recall)} | {_percent(mergewarden.quality.recall)} |",
        f"| F1 | {_percent(baseline.quality.f1)} | {_percent(mergewarden.quality.f1)} |",
        (
            "| High-severity Recall | "
            f"{_percent(baseline.quality.high_severity_recall)} | "
            f"{_percent(mergewarden.quality.high_severity_recall)} |"
        ),
        (
            "| False findings / PR | "
            f"{baseline.quality.false_findings_per_pr:.2f} | "
            f"{mergewarden.quality.false_findings_per_pr:.2f} |"
        ),
        "",
        "## Reliability",
        "",
        "| Metric | Baseline | MergeWarden |",
        "|---|---:|---:|",
        (
            "| Valid completion rate | "
            f"{_percent(baseline.reliability.valid_completion_rate)} | "
            f"{_percent(mergewarden.reliability.valid_completion_rate)} |"
        ),
        (
            "| Placeholder/incomplete runs | "
            f"{baseline.reliability.placeholder_incomplete_runs} | "
            f"{mergewarden.reliability.placeholder_incomplete_runs} |"
        ),
        (
            "| Workspace failures | "
            f"{baseline.reliability.workspace_failures} | "
            f"{mergewarden.reliability.workspace_failures} |"
        ),
        (
            "| Validator failures | "
            f"{baseline.reliability.validator_failures} | "
            f"{mergewarden.reliability.validator_failures} |"
        ),
        "",
        "## Per-fixture comparison",
        "",
        "| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |",
        "|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for spec in report.fixtures:
        left = _representative_run(report.runs, spec.fixture_id, "baseline")
        right = _representative_run(report.runs, spec.fixture_id, "mergewarden")
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{spec.fixture_id}`",
                    str(len(spec.gold_findings)),
                    _hit_cell(left),
                    _hit_cell(right),
                    _fp_cell(left),
                    _fp_cell(right),
                    "yes"
                    if left
                    and right
                    and left.valid_completion
                    and right.valid_completion
                    else "no",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Main failure mode",
            "",
            _main_failure_mode(report),
            "",
            "## Controls and optional oracles",
            "",
            (
                f"本轮保留 {report.clean_control_count} 个已稳定零问题 controls；"
                "未把它们包装成并不存在的 paired repair。"
            ),
            "Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。",
            "",
            "## Deliberately deferred",
            "",
            "- 不为全部 case 默认运行 3 repeats；仅 runtime instability 才重试。",
            "- 不增加统计显著性、几十个 fixtures 或复杂 composite score。",
            "- 不为所有 findings 建 executable oracle 或 Docker benchmark。",
            "- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。",
            "",
            "## README conclusion",
            "",
            report.readme_conclusion,
            "",
        ]
    )
    return "\n".join(lines)


def _representative_run(
    runs: list[CoreRunRecord],
    fixture_id: str,
    label: CoreVariantLabel,
) -> CoreRunRecord | None:
    selected = [
        item
        for item in runs
        if item.fixture_id == fixture_id and item.variant_label == label
    ]
    valid = [item for item in selected if item.valid_completion]
    return valid[-1] if valid else (selected[-1] if selected else None)


def _hit_cell(record: CoreRunRecord | None) -> str:
    if record is None or not record.valid_completion or record.quality is None:
        return "invalid"
    return f"{record.quality.matched_count}/{record.quality.gold_count}"


def _fp_cell(record: CoreRunRecord | None) -> str:
    if record is None or not record.valid_completion or record.quality is None:
        return "—"
    return str(record.quality.false_finding_count)


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _main_failure_mode(report: CoreEvalReport) -> str:
    candidate_attempts = [item for item in report.runs if item.role == "candidate"]
    candidate_runs = [item for item in candidate_attempts if item.valid_completion]
    candidate_ids = {item.fixture_id for item in candidate_attempts}
    missing_by_label: dict[CoreVariantLabel, int] = {
        "baseline": 0,
        "mergewarden": 0,
    }
    for label in missing_by_label:
        missing_by_label[label] = sum(
            not any(
                item.fixture_id == fixture_id
                and item.variant_label == label
                and item.valid_completion
                for item in candidate_attempts
            )
            for fixture_id in candidate_ids
        )
    missing_cells = sum(missing_by_label.values())
    if missing_cells:
        incomplete = [item for item in candidate_attempts if not item.valid_completion]
        no_submit = sum(not item.result.submit_review_seen_any for item in incomplete)
        hard_capped = sum(
            "budget_hard_capped" in item.result.finish_reasons for item in incomplete
        )
        missing_summary = "、".join(
            f"{label} {count} 个" for label, count in missing_by_label.items() if count
        )
        return (
            f"{len(incomplete)}/{len(candidate_attempts)} 个 candidate attempts 未合法完成，"
            f"其中 {no_submit} 个没有 submit_review，{hard_capped} 个在 hard token cap 后结束；"
            f"{missing_summary} candidate fixtures 缺少 valid completion。当前首要问题是 "
            "runtime reliability 与完整 PR 上下文的预算伸缩，而不是 semantic judge；"
            "review quality A/B 暂不可比较。"
        )
    raw_findings = sum(
        item.result.process_metrics.model_raw_issue_count for item in candidate_runs
    )
    filtered_runs = sum(
        item.result.process_metrics.model_raw_issue_count > 0
        and item.result.process_metrics.final_effective_issue_count == 0
        for item in candidate_runs
    )
    semantic_rejections = sum(
        item.result.process_metrics.raw_verifier_rejected_count
        for item in candidate_runs
    )
    deterministic_rejections = sum(
        item.result.process_metrics.deterministic_evidence_rejected_count
        for item in candidate_runs
    )
    if raw_findings and filtered_runs:
        return (
            f"{len(candidate_runs)} 个 valid candidate runs 共提出 {raw_findings} 条 raw findings，"
            f"但其中 {filtered_runs} 个 runs 在最终输出前被过滤；语义 verifier 拒绝 "
            f"{semantic_rejections} 条，deterministic evidence gate 拒绝 "
            f"{deterministic_rejections} 条。当前首要问题是 verifier/evidence 链造成的 recall loss，"
            "而不是 workspace、completion 或 semantic judge failure。"
        )
    if not candidate_runs:
        return "Candidate runs 没有合法完成，当前首要问题是 runtime reliability。"
    return "当前 candidate runs 没有形成可归纳的单一 failure mode。"


def _readme_conclusion(report: CoreEvalReport) -> str:
    by_label = {item.label: item for item in report.variants}
    baseline = by_label["baseline"]
    mergewarden = by_label["mergewarden"]
    left_f1 = baseline.quality.f1
    right_f1 = mergewarden.quality.f1
    if left_f1 is not None and right_f1 is not None and right_f1 > left_f1:
        comparison = f"当前 MergeWarden 的 F1 为 {right_f1:.1%}，高于简单 baseline 的 {left_f1:.1%}"
    elif left_f1 is not None and right_f1 is not None and right_f1 < left_f1:
        comparison = f"当前 MergeWarden 的 F1 为 {right_f1:.1%}，低于简单 baseline 的 {left_f1:.1%}"
    elif left_f1 is not None and right_f1 is not None:
        comparison = f"当前 A/B 的 F1 同为 {right_f1:.1%}，尚未显示稳定优势"
    else:
        comparison = "当前合法完成不足，尚不能比较 A/B review quality"
    return (
        f"在 {report.core_fixture_count} 个 real-world full-workspace PR 组成的小型精选集上，"
        f"{comparison}；MergeWarden valid completion rate 为 "
        f"{mergewarden.reliability.valid_completion_rate:.1%}。"
        "该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。"
    )


def save_core_report(
    report: CoreEvalReport,
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> tuple[Path, Path]:
    """Persist machine JSON and the concise Markdown presentation."""
    resolved_json = Path(json_path)
    resolved_markdown = Path(markdown_path)
    resolved_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_markdown.parent.mkdir(parents=True, exist_ok=True)
    resolved_json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    resolved_markdown.write_text(render_core_report(report), encoding="utf-8")
    return resolved_json, resolved_markdown


@click.group()
def main() -> None:
    """Run or render the small Core Eval v1."""


@main.command("audit")
@click.option(
    "--config",
    "config_path",
    default="eval/core_eval_v1.yaml",
    type=click.Path(exists=True, dir_okay=False),
)
def audit_cmd(config_path: str) -> None:
    """Validate the curated config and full-workspace fixture contracts."""
    config = load_core_config(config_path)
    loaded = load_core_fixtures(config, repo_root=Path.cwd())
    click.echo(
        f"Core Eval audit PASS: {len(loaded)} full-workspace fixtures, "
        f"{sum(spec.role == 'candidate' for spec, _ in loaded)} candidates."
    )


@main.command("run")
@click.option(
    "--config",
    "config_path",
    default="eval/core_eval_v1.yaml",
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--output-json",
    default="eval/outputs/core-eval-v1.json",
    type=click.Path(dir_okay=False),
)
@click.option(
    "--output-markdown",
    default="eval/reports/core-eval-v1.md",
    type=click.Path(dir_okay=False),
)
def run_cmd(config_path: str, output_json: str, output_markdown: str) -> None:
    """Execute one A/B pass and retry only runtime instability."""
    config = load_core_config(config_path)
    report = asyncio.run(
        run_core_eval(
            config,
            config_path=config_path,
            repo_root=Path.cwd(),
            progress=click.echo,
        )
    )
    json_path, markdown_path = save_core_report(
        report,
        json_path=output_json,
        markdown_path=output_markdown,
    )
    click.echo(render_core_report(report))
    click.echo(f"JSON: {json_path}")
    click.echo(f"Markdown: {markdown_path}")


@main.command("report")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True))
def report_cmd(input_path: str) -> None:
    """Render a saved Core Eval report without rerunning models."""
    report = CoreEvalReport.model_validate_json(
        Path(input_path).read_text(encoding="utf-8")
    )
    click.echo(render_core_report(report))


if __name__ == "__main__":
    main()
