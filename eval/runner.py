"""Evaluation runner for golden fixtures."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from time import perf_counter

from eval.schemas import EvalIssueMatch, EvalResult, Fixture
from src.analyzer.output_formatter import Severity
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.orchestrator.agent_loop import AgentOrchestrator


def load_fixtures(
    fixtures_dir: str | Path = Path("eval") / "fixtures",
    *,
    suite: str = "golden",
    reviewed_only: bool = True,
) -> list[Fixture]:
    """Load fixtures for one suite."""
    root = Path(fixtures_dir)
    fixtures: list[Fixture] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "manifest.json":
            continue
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        if fixture.metadata.suite != suite:
            continue
        if reviewed_only and not fixture.metadata.reviewed:
            continue
        fixtures.append(fixture)
    return fixtures


async def run_single(fixture: Fixture) -> EvalResult:
    """Run one fixture and return evaluation metadata."""
    expected_count = len(fixture.expected.issues)
    try:
        with tempfile.TemporaryDirectory(prefix="eval-fixture-") as tmp_dir:
            repo_root = Path(tmp_dir)
            _write_fixture_files(repo_root, fixture.input.files)
            orchestrator = AgentOrchestrator(permission_mode="default")

            start = perf_counter()
            if fixture.type == "review":
                request = ReviewRequest(
                    repo_path=str(repo_root),
                    diff_mode=bool(fixture.input.diff_text),
                    diff_text=fixture.input.diff_text or None,
                    verbose=False,
                )
                response = await orchestrator.run_review(request)
                parsed = ReviewResponse.model_validate(response.model_dump())
                actual_issues = parsed.report.issues
            else:
                request = DebugRequest(
                    repo_path=str(repo_root),
                    error_log_text=fixture.input.error_log,
                    verbose=False,
                )
                response = await orchestrator.run_debug(request)
                parsed = DebugResponse.model_validate(response.model_dump())
                actual_issues = parsed.steps
            latency = perf_counter() - start

            total_tokens = _read_total_tokens(repo_root, parsed.run_id)
            matches, matched_count, false_positive_count = _match_issues(fixture, parsed)
            raw_output = parsed.model_dump(mode="json")

            return EvalResult(
                fixture_id=fixture.id,
                fixture_type=fixture.type,
                run_id=parsed.run_id,
                schema_valid=True,
                expected_count=expected_count,
                actual_count=len(actual_issues),
                matched_count=matched_count,
                false_positive_count=false_positive_count,
                latency_seconds=latency,
                total_tokens=total_tokens,
                issue_matches=matches,
                raw_output=raw_output,
            )
    except Exception as exc:  # noqa: BLE001
        return EvalResult(
            fixture_id=fixture.id,
            fixture_type=fixture.type,
            schema_valid=False,
            expected_count=expected_count,
            error=str(exc),
        )


async def run_suite(fixtures: list[Fixture]) -> list[EvalResult]:
    """Run all fixtures sequentially."""
    results: list[EvalResult] = []
    for fixture in fixtures:
        results.append(await run_single(fixture))
    return results


def _write_fixture_files(repo_root: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        safe_rel = rel_path.replace("\\", "/").lstrip("/")
        target = repo_root / safe_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


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
            if not _location_matches(expected_issue.location_pattern, location):
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



