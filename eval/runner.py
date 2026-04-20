"""Evaluation runner for golden fixtures."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path
from statistics import mean, pstdev
from time import perf_counter
from typing import Any

from eval.schemas import (
    EvalIssueMatch,
    EvalResult,
    Fixture,
    FixtureManifest,
    SampledFixtureResult,
)
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import Severity
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.orchestrator.agent_loop import AgentOrchestrator

EVAL_EVENT_LOGS_OUTPUT_DIR = Path("eval") / "outputs" / "event_logs"


def load_fixtures(
    fixtures_dir: str | Path = Path("eval") / "fixtures",
    *,
    suite: str = "golden",
    reviewed_only: bool = True,
) -> list[Fixture]:
    """Load fixtures for one suite."""
    root = Path(fixtures_dir)
    fixture_paths = _resolve_fixture_paths(root)
    fixtures: list[Fixture] = []
    for path in fixture_paths:
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        if fixture.metadata.suite != suite:
            continue
        if reviewed_only and not fixture.metadata.reviewed:
            continue
        fixtures.append(fixture)
    return fixtures


def _resolve_fixture_paths(root: Path) -> list[Path]:
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = FixtureManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            resolved: list[Path] = []
            for entry in manifest.entries:
                candidate = Path(entry.path)
                if not candidate.is_absolute():
                    candidate = (root.parent / candidate).resolve()
                if candidate.exists() and candidate.suffix == ".json":
                    resolved.append(candidate)
            if resolved:
                return sorted(resolved)
        except Exception:  # noqa: BLE001
            pass
    return sorted(path for path in root.glob("*.json") if path.name != "manifest.json")


async def run_single(fixture: Fixture, *, temperature: float = 0.0) -> EvalResult:
    """Run one fixture and return evaluation metadata."""
    expected_count = len(fixture.expected.issues)
    try:
        with tempfile.TemporaryDirectory(prefix="eval-fixture-") as tmp_dir:
            repo_root = Path(tmp_dir)
            _write_fixture_files(repo_root, fixture.input.files)
            orchestrator = AgentOrchestrator(permission_mode="default", temperature=temperature)
            sandbox_context = _build_sandbox_context(fixture.input.files, repo_root)

            start = perf_counter()
            parsed_response: ReviewResponse | DebugResponse
            actual_count = 0
            if fixture.type == "review":
                original_diff = fixture.input.diff_text or ""
                review_request = ReviewRequest(
                    repo_path=str(repo_root),
                    diff_mode=bool(original_diff),
                    diff_text=_prepend_context(original_diff, sandbox_context),
                    verbose=False,
                )
                review_response = await orchestrator.run_review(review_request)
                parsed_response = ReviewResponse.model_validate(review_response.model_dump())
                actual_count = len(parsed_response.report.issues)
            else:
                original_error_log = fixture.input.error_log or ""
                debug_request = DebugRequest(
                    repo_path=str(repo_root),
                    error_log_text=_prepend_context(original_error_log, sandbox_context),
                    verbose=False,
                )
                debug_response = await orchestrator.run_debug(debug_request)
                parsed_response = DebugResponse.model_validate(debug_response.model_dump())
                actual_count = len(parsed_response.steps)
            latency = perf_counter() - start

            total_tokens = _read_total_tokens(repo_root, parsed_response.run_id)
            log_stats = _read_event_log_stats(repo_root, parsed_response.run_id)
            resolved_log = _resolve_event_log_path(repo_root, parsed_response.run_id)
            event_log_path = _persist_event_log_to_outputs(
                Path(resolved_log) if resolved_log else None,
                fixture.id,
                parsed_response.run_id,
            )
            matches, matched_count, false_positive_count = _match_issues(fixture, parsed_response)
            raw_output = parsed_response.model_dump(mode="json")

            placeholder = _is_placeholder_response(parsed_response)
            empty_business_output = _is_empty_business_output(parsed_response)
            return EvalResult(
                fixture_id=fixture.id,
                fixture_type=fixture.type,
                run_id=parsed_response.run_id,
                schema_valid=not empty_business_output,
                expected_count=expected_count,
                actual_count=actual_count,
                matched_count=matched_count,
                false_positive_count=false_positive_count,
                latency_seconds=latency,
                total_tokens=total_tokens,
                event_log_path=event_log_path,
                error=(
                    "Empty review output: no summary or issues."
                    if empty_business_output
                    else None
                ),
                issue_matches=matches,
                raw_output=raw_output,
                placeholder_summary=placeholder,
                submit_review_seen_any=log_stats["submit_review_seen_any"],
                submit_debug_seen_any=log_stats["submit_debug_seen_any"],
                budget_exhausted=log_stats["budget_exhausted"],
                budget_state=log_stats["budget_state"],
                finish_reasons=log_stats["finish_reasons"],
            )
    except Exception as exc:  # noqa: BLE001
        return EvalResult(
            fixture_id=fixture.id,
            fixture_type=fixture.type,
            schema_valid=False,
            expected_count=expected_count,
            error=str(exc),
        )


async def run_suite(
    fixtures: list[Fixture],
    *,
    samples: int = 1,
    concurrency: int = 1,
    temperature: float = 0.0,
) -> list[SampledFixtureResult]:
    """Run all fixtures with optional K-sample aggregation."""
    sampled_results: list[SampledFixtureResult] = []
    for fixture in fixtures:
        sampled_results.append(
            await run_single_sampled(
                fixture,
                samples=samples,
                concurrency=concurrency,
                temperature=temperature,
            )
        )
    return sampled_results


async def run_single_sampled(
    fixture: Fixture,
    *,
    samples: int,
    concurrency: int,
    temperature: float,
) -> SampledFixtureResult:
    """Run one fixture K times and aggregate stability metrics."""
    sample_count = max(1, samples)
    max_concurrency = max(1, min(concurrency, sample_count))
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run() -> EvalResult:
        async with semaphore:
            return await run_single(fixture, temperature=temperature)

    runs = await asyncio.gather(*(_run() for _ in range(sample_count)))
    return _aggregate_sampled_result(fixture, runs)


def _aggregate_sampled_result(
    fixture: Fixture,
    runs: list[EvalResult],
) -> SampledFixtureResult:
    expected_count = len(fixture.expected.issues)
    hit_rates = [
        _compute_hit_rate(run.matched_count, run.expected_count or expected_count) for run in runs
    ]
    fp_rates = [_compute_false_positive_rate(run.false_positive_count, run.actual_count) for run in runs]
    pass_at_k = 1.0 if expected_count == 0 else max(hit_rates, default=0.0)
    schema_valid_rate = (
        sum(1 for run in runs if run.schema_valid and not run.placeholder_summary)
        / len(runs)
        if runs
        else 0.0
    )
    return SampledFixtureResult(
        fixture_id=fixture.id,
        fixture_type=fixture.type,
        expected_count=expected_count,
        samples=len(runs) or 1,
        runs=runs,
        pass_at_k_hit_rate=pass_at_k,
        mean_hit_rate=float(mean(hit_rates)) if hit_rates else 0.0,
        hit_rate_stddev=float(pstdev(hit_rates)) if len(hit_rates) > 1 else 0.0,
        mean_false_positive_rate=float(mean(fp_rates)) if fp_rates else 0.0,
        worst_hit_rate=min(hit_rates) if hit_rates else 0.0,
        best_hit_rate=max(hit_rates) if hit_rates else 0.0,
        schema_valid_rate=schema_valid_rate,
    )


def _compute_hit_rate(matched_count: int, expected_count: int) -> float:
    if expected_count <= 0:
        return 1.0
    return max(0.0, min(1.0, matched_count / expected_count))


def _compute_false_positive_rate(false_positive_count: int, actual_count: int) -> float:
    if actual_count <= 0:
        return 0.0
    return max(0.0, min(1.0, false_positive_count / actual_count))


def _write_fixture_files(repo_root: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        safe_rel = rel_path.replace("\\", "/").lstrip("/")
        target = repo_root / safe_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _build_sandbox_context(files: dict[str, str], repo_root: Path) -> str:
    lines = [
        "[SANDBOX CONTEXT]",
        "This run uses a sparse fixture sandbox, not a full repository clone.",
        f"Workspace root (use as base for all file paths): {repo_root}",
        "Only these files are available:",
    ]
    for rel_path in sorted(files):
        safe_rel = rel_path.replace("\\", "/").lstrip("/")
        lines.append(f"- {safe_rel}")
    lines.extend(
        [
            "Before deep search, prefer list_dir to verify a directory exists.",
            "[END SANDBOX CONTEXT]",
        ]
    )
    return "\n".join(lines)


def _prepend_context(original_text: str, sandbox_context: str) -> str:
    body = original_text.strip()
    if body:
        return f"{sandbox_context}\n\n{body}"
    return sandbox_context


_PLACEHOLDER_REVIEW_SUMMARY = "Review pipeline completed with placeholder summary."
_PLACEHOLDER_DEBUG_SUMMARY = "Debug pipeline completed with placeholder summary."


def _is_placeholder_response(parsed: ReviewResponse | DebugResponse) -> bool:
    if isinstance(parsed, ReviewResponse):
        return (
            parsed.report.summary.strip() == _PLACEHOLDER_REVIEW_SUMMARY
            and not parsed.report.issues
        )
    return (
        parsed.summary.strip() == _PLACEHOLDER_DEBUG_SUMMARY and not parsed.steps
    )


def _is_empty_business_output(parsed: ReviewResponse | DebugResponse) -> bool:
    if isinstance(parsed, ReviewResponse):
        if _is_placeholder_response(parsed):
            return False
        return not parsed.report.summary.strip() and not parsed.report.issues
    return False


def _read_event_log_stats(repo_root: Path, run_id: str) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "submit_review_seen_any": False,
        "submit_debug_seen_any": False,
        "budget_exhausted": False,
        "budget_state": "none",
        "finish_reasons": [],
    }
    settings = get_settings()
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = repo_root / log_dir
    log_path = log_dir / f"{run_id}.jsonl"
    if not log_path.exists():
        return stats
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        etype = event.get("event_type")
        payload = event.get("payload", {}) or {}
        if etype == "decision":
            reason = str(payload.get("reason", "")).strip()
            if reason:
                stats["finish_reasons"].append(reason)
            if payload.get("submit_review_seen_any"):
                stats["submit_review_seen_any"] = True
            if payload.get("submit_debug_seen_any"):
                stats["submit_debug_seen_any"] = True
            bs = str(payload.get("budget_state", "")).strip()
            if bs and bs != "none":
                stats["budget_state"] = bs
            if payload.get("budget_exhausted"):
                stats["budget_exhausted"] = True
        elif etype == "plan_parsed":
            if payload.get("submit_review_seen"):
                stats["submit_review_seen_any"] = True
            if payload.get("submit_debug_seen"):
                stats["submit_debug_seen_any"] = True
    return stats


def _read_total_tokens(repo_root: Path, run_id: str) -> int:
    settings = get_settings()
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = repo_root / log_dir
    log_path = log_dir / f"{run_id}.jsonl"
    if not log_path.exists():
        return 0

    total = 0
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "model_call":
            continue
        payload = event.get("payload", {})
        total += int(payload.get("tokens", 0) or 0)
    return total


def _resolve_event_log_path(repo_root: Path, run_id: str) -> str | None:
    if not run_id.strip():
        return None
    settings = get_settings()
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = repo_root / log_dir
    log_path = log_dir / f"{run_id}.jsonl"
    if not log_path.exists():
        return None
    return str(log_path)


def _sanitize_fixture_id_for_filename(fixture_id: str) -> str:
    return fixture_id.replace("\\", "_").replace("/", "_")


def _persist_event_log_to_outputs(
    src: Path | None,
    fixture_id: str,
    run_id: str,
) -> str | None:
    """Copy event log into eval/outputs/event_logs; return absolute path or None."""
    if not run_id.strip() or src is None:
        return None
    if not src.is_file():
        return None
    safe_fid = _sanitize_fixture_id_for_filename(fixture_id)
    dest_dir = EVAL_EVENT_LOGS_OUTPUT_DIR
    dest = dest_dir / f"{safe_fid}_{run_id}.jsonl"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError:
        return None
    return str(dest.resolve())


def _severity_rank(value: str) -> int:
    levels = {
        Severity.CRITICAL.value: 4,
        Severity.WARNING.value: 3,
        Severity.INFO.value: 2,
        Severity.STYLE.value: 1,
    }
    return levels.get(value, 0)


def _match_issues(
    fixture: Fixture,
    response: ReviewResponse | DebugResponse,
) -> tuple[list[EvalIssueMatch], int, int]:
    expected = fixture.expected.issues
    if isinstance(response, ReviewResponse):
        actual_locations = [issue.location for issue in response.report.issues]
        actual_severity = [issue.severity.value for issue in response.report.issues]
    else:
        actual_locations = [step.location for step in response.steps]
        actual_severity = ["warning" for _ in response.steps]

    used_actual_indices: set[int] = set()
    matches: list[EvalIssueMatch] = []
    matched_count = 0
    for idx, expected_issue in enumerate(expected):
        hit_index: int | None = None
        for actual_idx, location in enumerate(actual_locations):
            if actual_idx in used_actual_indices:
                continue
            semantic_hit = _semantic_location_matches(expected_issue, location)
            legacy_hit = _location_matches(expected_issue.location_pattern, location)
            if not semantic_hit and not legacy_hit:
                continue
            if _severity_rank(actual_severity[actual_idx]) < _severity_rank(
                expected_issue.severity.value
            ):
                continue
            hit_index = actual_idx
            used_actual_indices.add(actual_idx)
            break
        matched = hit_index is not None
        if matched:
            matched_count += 1
        matches.append(
            EvalIssueMatch(
                expected_index=idx,
                matched=matched,
                matched_actual_index=hit_index,
            )
        )
    false_positive_count = max(0, len(actual_locations) - matched_count)
    return matches, matched_count, false_positive_count


def _location_matches(pattern: str, location: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, location) is not None
    except re.error:
        return pattern in location


def _semantic_location_matches(expected_issue: Any, location: str) -> bool:
    expected_path = str(getattr(expected_issue, "path", "") or "").strip().replace("\\", "/")
    raw_expected_line = getattr(expected_issue, "line", None)
    raw_expected_end_line = getattr(expected_issue, "end_line", None)
    expected_line = raw_expected_line if isinstance(raw_expected_line, int) else None
    expected_end_line = raw_expected_end_line if isinstance(raw_expected_end_line, int) else None
    if not expected_path and expected_line is None and expected_end_line is None:
        return False
    parsed = normalize_location(location)
    if not parsed.valid:
        return False
    if expected_path and parsed.path != expected_path:
        return False
    if expected_line is None:
        return True
    actual_start = parsed.line or expected_line
    actual_end = parsed.end_line or actual_start
    expected_end = expected_end_line or expected_line
    return actual_start <= expected_end and actual_end >= expected_line



