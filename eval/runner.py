"""Evaluation runner for golden fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from statistics import mean, pstdev
from time import perf_counter, sleep
from typing import Any

from eval.run_summary import extract_review_process_metrics
from eval.schemas import (
    EVAL_MATCHER_VERSION,
    EvalIssueMatch,
    EvalResult,
    EvalVariant,
    Fixture,
    FixtureManifest,
    FixtureWorkspace,
    SampledFixtureResult,
)
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import (
    Severity,
    has_specific_code_evidence,
    has_specific_diff_evidence,
)
from src.analyzer.persistent_index import INDEX_SCHEMA_VERSION
from src.analyzer.schemas import (
    DebugRequest,
    DebugResponse,
    ReviewRequest,
    ReviewResponse,
)
from src.config import get_settings
from src.orchestrator.agent_loop import AgentOrchestrator

EVAL_EVENT_LOGS_OUTPUT_DIR = Path("eval") / "outputs" / "event_logs"
EVAL_WORKSPACE_CACHE_DIR = Path("eval") / "outputs" / "workspace_cache"
_MIN_CRITICAL_CONFIDENCE = 0.85
_MIN_WARNING_CONFIDENCE = 0.85
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()
_EVAL_EXPECTED_LOCATION_WARNING_CONFIDENCE = 0.7


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


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    allow_lazy_fetch: bool = True,
) -> str:
    timeout = get_settings().eval_git_timeout_seconds
    settings = get_settings()
    command = ["git"]
    if settings.eval_git_ssl_backend != "system":
        command.extend(["-c", f"http.sslBackend={settings.eval_git_ssl_backend}"])
    if cwd is not None:
        command.extend(["-c", f"safe.directory={cwd.resolve()}"])
    command.extend(args)
    environment = None
    if not allow_lazy_fetch:
        environment = os.environ.copy()
        environment["GIT_NO_LAZY_FETCH"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            input=input_text,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        command = "git " + " ".join(args)
        raise TimeoutError(f"{command} timed out after {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        command = "git " + " ".join(args)
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        details = stderr or stdout or str(exc)
        raise RuntimeError(
            f"{command} failed with exit code {exc.returncode}: {details}"
        ) from exc
    return completed.stdout.strip()


def _prepare_fixture_workspace(
    fixture: Fixture,
    target_root: Path,
    *,
    workspace_cache_dir: Path | None = None,
) -> Path:
    """Restore the workspace for one fixture and return the repo root."""
    workspace = fixture.input.workspace
    if workspace is None:
        target_root.mkdir(parents=True, exist_ok=True)
        _write_fixture_files(target_root, fixture.input.files)
        return target_root
    if workspace.kind == "git":
        repo_root = _checkout_git_workspace(
            workspace,
            target_root,
            pr_number=fixture.source.pr_number,
            workspace_cache_dir=workspace_cache_dir,
            offline=get_settings().eval_offline_workspace_cache,
        )
        if workspace.apply_fixture_diff:
            _apply_fixture_diff(fixture, repo_root)
        return repo_root
    raise ValueError(f"Unsupported fixture workspace kind: {workspace.kind}")


def _apply_fixture_diff(fixture: Fixture, repo_root: Path) -> None:
    """Apply a fixture patch without moving the restored workspace HEAD."""
    diff_text = fixture.input.diff_text
    if fixture.type != "review" or not diff_text.strip():
        raise ValueError(
            f"Fixture {fixture.id} requires a non-empty review diff to apply"
        )
    patch_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            suffix=".diff",
            delete=False,
        ) as handle:
            handle.write(diff_text)
            if not diff_text.endswith("\n"):
                handle.write("\n")
            patch_path = Path(handle.name)
        _run_git(
            ["apply", "--check", "--whitespace=nowarn", "--", str(patch_path)],
            cwd=repo_root,
        )
        _run_git(
            ["apply", "--whitespace=nowarn", "--", str(patch_path)],
            cwd=repo_root,
        )
    except (RuntimeError, TimeoutError) as exc:
        raise RuntimeError(
            f"Failed to apply fixture diff for {fixture.id}: {exc}"
        ) from exc
    finally:
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)


def _checkout_git_workspace(
    workspace: FixtureWorkspace,
    target_root: Path,
    *,
    pr_number: int | None = None,
    workspace_cache_dir: Path | None = None,
    offline: bool = False,
) -> Path:
    target_root.parent.mkdir(parents=True, exist_ok=True)
    if target_root.exists():
        shutil.rmtree(target_root)
    cache_root = (
        _ensure_git_workspace_cache(
            workspace,
            workspace_cache_dir,
            pr_number=pr_number,
            offline=offline,
        )
        if workspace_cache_dir is not None
        else None
    )
    if cache_root is not None:
        try:
            _run_git(
                [
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--shared",
                    "--branch",
                    _cache_snapshot_branch(workspace.checkout_sha),
                    str(cache_root),
                    str(target_root),
                ],
                allow_lazy_fetch=False,
            )
        except (RuntimeError, TimeoutError) as exc:
            if target_root.exists():
                _remove_path_with_retries(target_root)
            mode = "offline " if offline else ""
            raise RuntimeError(f"{mode}cache clone failed: {exc}") from exc
    if cache_root is None:
        if offline:
            raise RuntimeError(f"offline cache miss: {workspace.repo_url}")
        _run_git(
            [
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--depth=1",
                workspace.repo_url,
                str(target_root),
            ]
        )
    try:
        _run_git(
            ["checkout", "--quiet", workspace.checkout_sha],
            cwd=target_root,
            allow_lazy_fetch=cache_root is None,
        )
    except (RuntimeError, TimeoutError) as exc:
        if cache_root is not None or offline:
            raise RuntimeError(
                "offline cache is incomplete: "
                f"{workspace.repo_url} lacks materialized {workspace.checkout_sha}"
            ) from exc
        if pr_number is None:
            raise
        _run_git(
            [
                "fetch",
                "--quiet",
                "--filter=blob:none",
                "--depth=1",
                "origin",
                f"refs/pull/{pr_number}/head",
            ],
            cwd=target_root,
        )
        _run_git(["checkout", "--quiet", workspace.checkout_sha], cwd=target_root)
    checked_out = _run_git(["rev-parse", "HEAD"], cwd=target_root)
    if checked_out != workspace.checkout_sha:
        raise ValueError(
            "Workspace checkout mismatch: "
            f"expected {workspace.checkout_sha}, got {checked_out}"
        )
    return target_root


def _ensure_git_workspace_cache(
    workspace: FixtureWorkspace,
    workspace_cache_dir: Path,
    *,
    pr_number: int | None = None,
    offline: bool = False,
) -> Path:
    workspace_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_root = workspace_cache_dir / _workspace_cache_key(workspace.repo_url)
    lock = _workspace_cache_lock(str(cache_root))
    with lock:
        if offline:
            if not _is_valid_bare_cache(cache_root):
                raise RuntimeError(f"offline cache miss: {workspace.repo_url}")
            try:
                _verify_cache_snapshot_materialized(cache_root, workspace.checkout_sha)
            except (RuntimeError, TimeoutError) as exc:
                raise RuntimeError(
                    "offline cache miss: "
                    f"{workspace.repo_url} lacks materialized {workspace.checkout_sha}"
                ) from exc
            return cache_root
        if _is_valid_bare_cache(cache_root):
            try:
                _verify_cache_snapshot_materialized(cache_root, workspace.checkout_sha)
                return cache_root
            except (RuntimeError, TimeoutError):
                _fetch_cache_ref(
                    cache_root,
                    workspace.checkout_sha,
                    pr_number=pr_number,
                )
                _materialize_cache_snapshot(cache_root, workspace.checkout_sha)
                _verify_cache_snapshot_materialized(cache_root, workspace.checkout_sha)
                return cache_root
        if cache_root.exists():
            raise RuntimeError(f"Invalid workspace cache: {cache_root}")

        tmp_root = Path(
            tempfile.mkdtemp(
                prefix="wc-",
                suffix=".tmp",
                dir=workspace_cache_dir,
            )
        )
        try:
            _init_targeted_cache(tmp_root, workspace.repo_url)
            _fetch_cache_ref(
                tmp_root,
                workspace.checkout_sha,
                pr_number=pr_number,
            )
            _materialize_cache_snapshot(tmp_root, workspace.checkout_sha)
            _verify_cache_snapshot_materialized(tmp_root, workspace.checkout_sha)
            _publish_cache_with_retry(tmp_root, cache_root)
        except Exception:
            _cleanup_pack_temps(tmp_root)
            if tmp_root.exists():
                try:
                    _remove_path_with_retries(tmp_root, attempts=8)
                except OSError:
                    # A transport error is more actionable than a secondary
                    # best-effort cleanup failure caused by a transient pack lock.
                    pass
            raise
        return cache_root


def _is_valid_bare_cache(cache_root: Path) -> bool:
    if not (cache_root / "objects").is_dir() or not (cache_root / "config").is_file():
        return False
    try:
        return (
            _run_git(
                ["rev-parse", "--is-bare-repository"],
                cwd=cache_root,
                allow_lazy_fetch=False,
            )
            == "true"
        )
    except (RuntimeError, TimeoutError):
        return False


def _init_targeted_cache(cache_root: Path, repo_url: str) -> None:
    _run_git(["init", "--bare", "--quiet", str(cache_root)])
    _run_git(["remote", "add", "origin", repo_url], cwd=cache_root)
    _run_git(["config", "remote.origin.promisor", "true"], cwd=cache_root)
    _run_git(
        ["config", "remote.origin.partialclonefilter", "blob:none"], cwd=cache_root
    )
    _run_git(["config", "extensions.partialClone", "origin"], cwd=cache_root)


def _fetch_cache_ref(
    cache_root: Path,
    ref: str,
    *,
    pr_number: int | None = None,
) -> None:
    try:
        _run_git(
            ["cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=cache_root,
            allow_lazy_fetch=False,
        )
        _record_cache_snapshot_ref(cache_root, ref)
        return
    except (RuntimeError, TimeoutError):
        pass
    candidates = [ref]
    if pr_number is not None:
        candidates.append(f"refs/pull/{pr_number}/head")
    if not _is_github_remote(_cache_remote_url(cache_root)):
        # Local/file remotes commonly reject raw object-id wants and do not
        # expose GitHub PR refs. HEAD still fetches one targeted ref, and the
        # object-id check below prevents accepting the wrong snapshot.
        candidates.append("HEAD")

    errors: list[str] = []
    fetched_candidate: str | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            _run_git(
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--filter=blob:none",
                    "--depth=1",
                    "origin",
                    candidate,
                ],
                cwd=cache_root,
            )
            _run_git(
                ["cat-file", "-e", f"{ref}^{{commit}}"],
                cwd=cache_root,
                allow_lazy_fetch=False,
            )
            fetched_candidate = candidate
            break
        except (RuntimeError, TimeoutError) as exc:
            errors.append(f"{candidate}: {exc}")
            _cleanup_pack_temps(cache_root)
    else:
        raise RuntimeError(
            f"Unable to fetch targeted commit {ref}: " + " | ".join(errors)
        )
    assert fetched_candidate is not None
    # Re-fetch only the selected target without a filter. This materializes its
    # tree and blobs while retaining the partial-clone cache configuration for
    # later target commits; no unrelated refs are requested.
    _run_git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--refetch",
            "--no-filter",
            "--depth=1",
            "origin",
            fetched_candidate,
        ],
        cwd=cache_root,
    )
    _record_cache_snapshot_ref(cache_root, ref)


def _cache_snapshot_branch(checkout_sha: str) -> str:
    return f"eval-snapshot-{checkout_sha}"


def _record_cache_snapshot_ref(cache_root: Path, checkout_sha: str) -> None:
    _run_git(
        [
            "update-ref",
            f"refs/heads/{_cache_snapshot_branch(checkout_sha)}",
            checkout_sha,
        ],
        cwd=cache_root,
    )


def _cache_remote_url(cache_root: Path) -> str:
    return _run_git(["remote", "get-url", "origin"], cwd=cache_root)


def _is_github_remote(repo_url: str) -> bool:
    normalized = repo_url.replace("\\", "/").lower()
    return "github.com/" in normalized or normalized.startswith("git@github.com:")


def _materialize_cache_snapshot(cache_root: Path, checkout_sha: str) -> None:
    """Read every target-tree blob so the partial cache becomes offline-safe."""
    _run_git(
        [
            "archive",
            "--format=tar",
            f"--output={os.devnull}",
            checkout_sha,
        ],
        cwd=cache_root,
    )


def _verify_cache_snapshot_materialized(
    cache_root: Path,
    checkout_sha: str,
) -> None:
    _run_git(
        ["cat-file", "-e", f"{checkout_sha}^{{commit}}"],
        cwd=cache_root,
        allow_lazy_fetch=False,
    )
    _run_git(
        [
            "archive",
            "--format=tar",
            f"--output={os.devnull}",
            checkout_sha,
        ],
        cwd=cache_root,
        allow_lazy_fetch=False,
    )


def _is_windows_permission_error(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in {
        5,
        32,
    }


def _replace_with_retries(source: Path, destination: Path, attempts: int = 4) -> None:
    last_error: BaseException | None = None
    for index in range(max(1, attempts)):
        try:
            source.replace(destination)
            return
        except PermissionError as exc:
            last_error = exc
            if not _is_windows_permission_error(exc) or index + 1 >= attempts:
                raise
            sleep(0.05 * (index + 1))
    if last_error is not None:
        raise last_error


def _publish_cache_with_retry(
    source: Path, destination: Path, attempts: int = 4
) -> None:
    """Publish a completed mirror, tolerating transient Windows sharing locks."""
    _replace_with_retries(source, destination, attempts=attempts)


def _remove_path_with_retries(path: Path, attempts: int = 4) -> None:
    if not path.exists():
        return
    last_error: BaseException | None = None
    for index in range(max(1, attempts)):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except PermissionError as exc:
            last_error = exc
            if not _is_windows_permission_error(exc) or index + 1 >= attempts:
                raise
            sleep(0.05 * (index + 1))
    if last_error is not None:
        raise last_error


def _cleanup_pack_temps(cache_root: Path) -> None:
    pack_dir = cache_root / "objects" / "pack"
    if not pack_dir.is_dir():
        return
    for candidate in pack_dir.iterdir():
        if candidate.name.startswith("tmp_") or candidate.name.endswith(".tmp"):
            try:
                _remove_path_with_retries(candidate)
            except OSError:
                continue


def _workspace_cache_key(repo_url: str) -> str:
    digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_url.rstrip("/").removesuffix(".git"))
    return f"{stem[-48:]}_{digest}.git"


def _eval_relation_graph_index_path(fixture: Fixture) -> Path:
    """Return a stable eval-owned index path outside temporary checkouts."""
    workspace = fixture.input.workspace
    source = (
        workspace.repo_url
        if workspace is not None and workspace.repo_url.strip()
        else fixture.source.repo_full_name
    )
    source = source.strip() or fixture.id
    digest = hashlib.sha256(
        f"{source}|schema:{INDEX_SCHEMA_VERSION}".encode()
    ).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.rstrip("/").removesuffix(".git"))
    index_root = (
        Path(get_settings().eval_workspace_cache_dir) / "relation_index"
    ).resolve()
    return index_root / f"{stem[-64:]}_{digest}.sqlite3"


def _workspace_cache_lock(key: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CACHE_LOCKS[key] = lock
        return lock


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
            return sorted(
                path for path in root.glob("*.json") if path.name != "manifest.json"
            )
    return sorted(path for path in root.glob("*.json") if path.name != "manifest.json")


async def run_single(
    fixture: Fixture,
    *,
    temperature: float = 0.0,
    review_max_iterations: int | None = None,
    variant: EvalVariant | None = None,
) -> EvalResult:
    """Run one fixture and return evaluation metadata."""
    expected_count = len(fixture.expected.issues)
    selected_variant = variant or _default_eval_variant()
    stage_timings: dict[str, float] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="eval-fixture-") as tmp_dir:
            stage_started = perf_counter()
            repo_root = await asyncio.to_thread(
                _prepare_fixture_workspace,
                fixture,
                Path(tmp_dir) / "repo",
                workspace_cache_dir=Path(get_settings().eval_workspace_cache_dir),
            )
            stage_timings["prepare_workspace_seconds"] = perf_counter() - stage_started
            stage_started = perf_counter()
            diff_workspace_errors = await asyncio.to_thread(
                _validate_diff_added_lines_against_workspace,
                fixture,
                repo_root,
            )
            validation_errors = diff_workspace_errors + await asyncio.to_thread(
                _validate_expected_locations_against_diff,
                fixture,
                repo_root,
            )
            stage_timings["validate_fixture_seconds"] = perf_counter() - stage_started
            if validation_errors:
                return EvalResult(
                    fixture_id=fixture.id,
                    fixture_type=fixture.type,
                    **_variant_result_fields(selected_variant),
                    schema_valid=False,
                    expected_count=expected_count,
                    stage_timings=stage_timings,
                    error="; ".join(validation_errors),
                )
            relation_graph_index_path: Path | None = None
            if selected_variant.context_mode == "graph_hybrid":
                relation_graph_index_path = (
                    _eval_relation_graph_index_path(fixture)
                    if selected_variant.graph_cache_mode == "warm"
                    else repo_root / ".mergewarden" / "cold-eval-relation-index.sqlite3"
                )
            orchestrator = AgentOrchestrator(
                permission_mode="default",
                temperature=temperature,
                review_max_iterations=_effective_review_max_iterations(
                    review_max_iterations
                ),
                review_min_tool_iterations=max(
                    1, get_settings().eval_review_min_tool_iterations
                ),
                review_diff_first_changed_files=True,
                relation_graph_index_path=relation_graph_index_path,
                context_mode=selected_variant.context_mode,
            )
            sandbox_context = _build_fixture_context(fixture, repo_root)

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
                parsed_response = ReviewResponse.model_validate(
                    review_response.model_dump()
                )
                actual_count = len(_effective_review_issues(fixture, parsed_response))
            else:
                original_error_log = fixture.input.error_log or ""
                debug_request = DebugRequest(
                    repo_path=str(repo_root),
                    error_log_text=_prepend_context(
                        original_error_log, sandbox_context
                    ),
                    verbose=False,
                )
                debug_response = await orchestrator.run_debug(debug_request)
                parsed_response = DebugResponse.model_validate(
                    debug_response.model_dump()
                )
                actual_count = len(parsed_response.steps)
            latency = perf_counter() - start
            stage_timings["agent_run_seconds"] = latency

            total_tokens = _read_total_tokens(repo_root, parsed_response.run_id)
            log_stats = _read_event_log_stats(repo_root, parsed_response.run_id)
            resolved_log = _resolve_event_log_path(repo_root, parsed_response.run_id)
            event_log_path = _persist_event_log_to_outputs(
                Path(resolved_log) if resolved_log else None,
                fixture.id,
                parsed_response.run_id,
            )
            matches, matched_count, false_positive_count = _match_issues(
                fixture, parsed_response
            )
            root_cause_quality = (
                _root_cause_quality(fixture, parsed_response, matches)
                if isinstance(parsed_response, ReviewResponse)
                else {}
            )
            raw_output = parsed_response.model_dump(mode="json")

            placeholder = _is_placeholder_response(parsed_response)
            empty_business_output = _is_empty_business_output(parsed_response)
            schema_valid = _eval_schema_valid(parsed_response)
            return EvalResult(
                fixture_id=fixture.id,
                fixture_type=fixture.type,
                **_variant_result_fields(selected_variant),
                run_id=parsed_response.run_id,
                schema_valid=schema_valid,
                expected_count=expected_count,
                actual_count=actual_count,
                matched_count=matched_count,
                false_positive_count=false_positive_count,
                **root_cause_quality,
                latency_seconds=latency,
                total_tokens=total_tokens,
                event_log_path=event_log_path,
                stage_timings=stage_timings,
                error=(
                    "Empty review output: no summary or issues."
                    if empty_business_output
                    else (
                        "Placeholder review output: no submit_review/debug before finalize."
                        if placeholder
                        else None
                    )
                ),
                issue_matches=matches,
                raw_output=raw_output,
                placeholder_summary=placeholder,
                submit_review_seen_any=log_stats["submit_review_seen_any"],
                submit_debug_seen_any=log_stats["submit_debug_seen_any"],
                budget_exhausted=log_stats["budget_exhausted"],
                budget_state=log_stats["budget_state"],
                finish_reasons=log_stats["finish_reasons"],
                workflow_invalid=(
                    review_response.workflow_invalid
                    if fixture.type == "review"
                    else False
                ),
                workflow_missing_steps=(
                    review_response.workflow_missing_steps
                    if fixture.type == "review"
                    else []
                ),
                process_metrics=extract_review_process_metrics(event_log_path),
            )
    except Exception as exc:  # noqa: BLE001
        return EvalResult(
            fixture_id=fixture.id,
            fixture_type=fixture.type,
            **_variant_result_fields(selected_variant),
            schema_valid=False,
            expected_count=expected_count,
            stage_timings=stage_timings,
            error=str(exc),
        )


async def run_suite(
    fixtures: list[Fixture],
    *,
    samples: int = 1,
    concurrency: int = 1,
    fixture_concurrency: int = 1,
    review_max_iterations: int | None = None,
    temperature: float = 0.0,
    variant: EvalVariant | None = None,
) -> list[SampledFixtureResult]:
    """Run all fixtures with optional K-sample aggregation."""
    max_fixture_concurrency = max(1, min(fixture_concurrency, len(fixtures) or 1))
    semaphore = asyncio.Semaphore(max_fixture_concurrency)

    async def _run_fixture(fixture: Fixture) -> SampledFixtureResult:
        async with semaphore:
            kwargs: dict[str, Any] = {
                "samples": samples,
                "concurrency": concurrency,
                "review_max_iterations": review_max_iterations,
                "temperature": temperature,
            }
            if variant is not None:
                kwargs["variant"] = variant
            return await run_single_sampled(fixture, **kwargs)

    return await asyncio.gather(*(_run_fixture(fixture) for fixture in fixtures))


async def run_single_sampled(
    fixture: Fixture,
    *,
    samples: int,
    concurrency: int,
    review_max_iterations: int | None = None,
    temperature: float = 0.0,
    variant: EvalVariant | None = None,
) -> SampledFixtureResult:
    """Run one fixture K times and aggregate stability metrics."""
    sample_count = max(1, samples)
    max_concurrency = max(1, min(concurrency, sample_count))
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run() -> EvalResult:
        async with semaphore:
            return await run_single(
                fixture,
                temperature=temperature,
                review_max_iterations=review_max_iterations,
                variant=variant,
            )

    runs = await asyncio.gather(*(_run() for _ in range(sample_count)))
    return _aggregate_sampled_result(fixture, runs)


def _aggregate_sampled_result(
    fixture: Fixture,
    runs: list[EvalResult],
) -> SampledFixtureResult:
    expected_count = len(fixture.expected.issues)
    hit_rates = [
        _compute_hit_rate(run.matched_count, run.expected_count or expected_count)
        for run in runs
    ]
    fp_rates = [
        _compute_false_positive_rate(run.false_positive_count, run.actual_count)
        for run in runs
    ]
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
        variant_id=(runs[0].variant_id if runs else ""),
        context_mode=(runs[0].context_mode if runs else "graph_hybrid"),
        graph_cache_mode=(runs[0].graph_cache_mode if runs else "warm"),
        matcher_version=EVAL_MATCHER_VERSION,
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


def _effective_review_max_iterations(configured: int | None) -> int:
    settings = get_settings()
    min_tool_iterations = settings.eval_review_min_tool_iterations
    requested = configured or settings.eval_review_max_iterations
    stable_cap = settings.eval_review_max_iterations_cap
    minimum_for_tool_feedback = max(2, min_tool_iterations + 1)
    effective_cap = max(minimum_for_tool_feedback, stable_cap)
    return max(minimum_for_tool_feedback, min(requested, effective_cap))


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


def _build_fixture_context(fixture: Fixture, repo_root: Path) -> str:
    if fixture.input.workspace is None:
        return _build_sandbox_context(fixture.input.files, repo_root)
    return _build_workspace_context(fixture.input.workspace, repo_root)


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


def _build_workspace_context(workspace: FixtureWorkspace, repo_root: Path) -> str:
    lines = [
        "[WORKSPACE CONTEXT]",
        "This run restored a full git workspace for the fixture.",
        f"Workspace root (use as base for all file paths): {repo_root}",
        f"Repository: {workspace.repo_url}",
        f"Checkout SHA: {workspace.checkout_sha}",
        "The review target remains the PR diff below; use read-only tools only for context.",
        "[END WORKSPACE CONTEXT]",
    ]
    return "\n".join(lines)


def _validate_expected_locations_against_diff(
    fixture: Fixture,
    repo_root: Path,
) -> list[str]:
    if (
        fixture.type != "review"
        or fixture.input.workspace is None
        or not fixture.input.diff_text.strip()
    ):
        return []
    changed_lines = _changed_new_lines_by_file(fixture.input.diff_text)
    errors: list[str] = []
    for issue in fixture.expected.issues:
        path = issue.path.strip().replace("\\", "/")
        if not path or issue.line is None:
            continue
        expected_start = issue.line
        expected_end = issue.end_line or expected_start
        changed_for_path = changed_lines.get(path, set())
        if not any(
            line in changed_for_path for line in range(expected_start, expected_end + 1)
        ):
            errors.append(
                f"Expected issue location is outside changed hunk: {path}:{expected_start}"
            )
            continue
        workspace_file = repo_root / path
        if not workspace_file.is_file():
            errors.append(f"Expected issue file is missing from workspace: {path}")
            continue
        line_count = len(workspace_file.read_text(encoding="utf-8").splitlines())
        if expected_start > line_count:
            errors.append(
                f"Expected issue line is outside workspace file: {path}:{expected_start}"
            )
    return errors


def _validate_diff_added_lines_against_workspace(
    fixture: Fixture,
    repo_root: Path,
) -> list[str]:
    if (
        fixture.type != "review"
        or fixture.input.workspace is None
        or not fixture.input.diff_text.strip()
    ):
        return []
    added_lines = _added_new_lines_by_file(fixture.input.diff_text)
    errors: list[str] = []
    for path, lines in added_lines.items():
        workspace_file = repo_root / path
        if not workspace_file.is_file():
            errors.append(f"Diff file is missing from workspace: {path}")
            continue
        workspace_lines = workspace_file.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        for line_number, expected_text in lines.items():
            actual_text = (
                workspace_lines[line_number - 1]
                if 1 <= line_number <= len(workspace_lines)
                else None
            )
            if actual_text != expected_text:
                errors.append(
                    "Fixture diff added line does not match workspace: "
                    f"{path}:{line_number}"
                )
                if len(errors) >= 10:
                    return errors
    return errors


def _changed_new_lines_by_file(diff_text: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    current_path = ""
    new_line: int | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            marker = raw_line[4:].strip()
            current_path = ""
            if marker != "/dev/null":
                current_path = marker.removeprefix("b/")
                changed.setdefault(current_path, set())
            new_line = None
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            new_line = int(match.group(1)) if match else None
            continue
        if not current_path or new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed[current_path].add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            new_line += 1
    return changed


def _added_new_lines_by_file(diff_text: str) -> dict[str, dict[int, str]]:
    added: dict[str, dict[int, str]] = {}
    current_path = ""
    new_line: int | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            marker = raw_line[4:].strip()
            current_path = ""
            if marker != "/dev/null":
                current_path = marker.removeprefix("b/")
                added.setdefault(current_path, {})
            new_line = None
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            new_line = int(match.group(1)) if match else None
            continue
        if not current_path or new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            added[current_path][new_line] = raw_line[1:]
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            new_line += 1
    return added


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
    return parsed.summary.strip() == _PLACEHOLDER_DEBUG_SUMMARY and not parsed.steps


def _eval_schema_valid(parsed: ReviewResponse | DebugResponse) -> bool:
    return not _is_empty_business_output(parsed) and not _is_placeholder_response(
        parsed
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
        if etype in {"decision", "phase_end"}:
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

    model_total = 0
    completed_total: int | None = None
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload", {})
        if event.get("event_type") == "model_call":
            model_total += int(payload.get("tokens", 0) or 0)
        elif (
            event.get("event_type") == "phase_end"
            and event.get("phase") == "review_complete"
        ):
            raw_total = payload.get("total_tokens")
            if isinstance(raw_total, int):
                completed_total = max(0, raw_total)
    return completed_total if completed_total is not None else model_total


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
        actual_issues = _effective_review_issues(fixture, response)
        actual_severity = [issue.severity.value for issue in actual_issues]
    else:
        actual_issues = response.steps
        actual_severity = ["warning" for _ in actual_issues]

    used_actual_indices: set[int] = set()
    matches: list[EvalIssueMatch] = []
    matched_count = 0
    for idx, expected_issue in enumerate(expected):
        hit_index: int | None = None
        for actual_idx, issue in enumerate(actual_issues):
            if actual_idx in used_actual_indices:
                continue
            if not _issue_matches_expected_location(expected_issue, issue):
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
    false_positive_count = max(0, len(actual_issues) - matched_count)
    return matches, matched_count, false_positive_count


def _effective_review_issues(fixture: Fixture, response: ReviewResponse) -> list[Any]:
    return [
        issue
        for issue in response.report.issues
        if (
            _is_eval_effective_issue(issue, fixture)
            or _is_eval_expected_location_issue(issue, fixture)
        )
        and _meets_expected_severity_floor(issue, fixture)
    ]


def _root_cause_quality(
    fixture: Fixture,
    response: ReviewResponse,
    matches: list[EvalIssueMatch],
) -> dict[str, int]:
    actual = _effective_review_issues(fixture, response)
    expected_root_keys = {
        index: (issue.root_cause_id.strip() or f"expected-{index}")
        for index, issue in enumerate(fixture.expected.issues)
    }
    actual_root_keys = {
        index: (issue.root_cause_id.strip() or f"actual-{index}")
        for index, issue in enumerate(actual)
    }
    expected_to_actual: dict[str, set[str]] = {}
    actual_to_expected: dict[str, set[str]] = {}
    repair_expected = 0
    repair_matched = 0
    for match in matches:
        expected = fixture.expected.issues[match.expected_index]
        expected_root = expected_root_keys[match.expected_index]
        if expected.repair_unit.strip():
            repair_expected += 1
        if not match.matched or match.matched_actual_index is None:
            continue
        actual_index = match.matched_actual_index
        actual_root = actual_root_keys[actual_index]
        expected_to_actual.setdefault(expected_root, set()).add(actual_root)
        actual_to_expected.setdefault(actual_root, set()).add(expected_root)
        if expected.repair_unit.strip() and _repair_unit_matches(
            expected.repair_unit, actual[actual_index]
        ):
            repair_matched += 1
    matched_roots = len(expected_to_actual)
    over_merge_count = sum(
        max(0, len(expected_roots) - 1)
        for expected_roots in actual_to_expected.values()
    )
    under_merge_count = sum(
        max(0, len(actual_roots) - 1) for actual_roots in expected_to_actual.values()
    )
    evidence_complete = sum(
        bool(issue.cause_evidence)
        and bool(issue.contract_evidence)
        and (not issue.trigger or bool(issue.trigger_evidence))
        and (not issue.impact or bool(issue.impact_evidence))
        for issue in actual
    )
    return {
        "expected_root_cause_count": len(set(expected_root_keys.values())),
        "matched_root_cause_count": matched_roots,
        "over_merge_count": over_merge_count,
        "under_merge_count": under_merge_count,
        "repair_unit_expected_count": repair_expected,
        "repair_unit_matched_count": repair_matched,
        "evidence_complete_count": evidence_complete,
        "final_finding_count": len(actual),
    }


def _repair_unit_matches(expected: str, issue: Any) -> bool:
    normalized_expected = " ".join(expected.strip().lower().replace("_", " ").split())
    repair = getattr(issue, "repair_intent", None)
    if repair is None:
        return False
    actual = " ".join(
        [
            str(getattr(repair, "action", "")),
            *[str(value) for value in getattr(repair, "targets", [])],
            str(getattr(repair, "boundary", "")),
            str(getattr(issue, "causal_mechanism", "")),
            str(getattr(issue, "violated_invariant", "")),
            str(getattr(issue, "suggestion", "")),
        ]
    )
    normalized_actual = " ".join(actual.lower().replace("_", " ").split())
    if (
        normalized_expected == normalized_actual
        or normalized_expected in normalized_actual
    ):
        return True
    expected_tokens = _semantic_match_tokens(expected) - {
        "a",
        "an",
        "at",
        "boundary",
        "the",
        "to",
    }
    actual_tokens = _semantic_match_tokens(actual)
    return bool(expected_tokens) and (
        len(expected_tokens & actual_tokens) / len(expected_tokens) >= 0.6
    )


def _meets_expected_severity_floor(issue: Any, fixture: Fixture) -> bool:
    if not fixture.expected.issues:
        return True
    expected_floor = min(
        _severity_rank(expected_issue.severity.value)
        for expected_issue in fixture.expected.issues
    )
    severity = str(
        getattr(getattr(issue, "severity", ""), "value", getattr(issue, "severity", ""))
    )
    return _severity_rank(severity) >= expected_floor


def _is_eval_expected_location_issue(issue: Any, fixture: Fixture) -> bool:
    if not fixture.expected.issues:
        return False
    severity = str(
        getattr(getattr(issue, "severity", ""), "value", getattr(issue, "severity", ""))
    )
    if severity != Severity.WARNING.value:
        return False
    confidence_raw = getattr(issue, "confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return False
    if confidence < _EVAL_EXPECTED_LOCATION_WARNING_CONFIDENCE:
        return False
    location = str(getattr(issue, "location", "") or "")
    return any(
        _semantic_location_matches(expected_issue, location)
        or _location_matches(expected_issue.location_pattern, location)
        for expected_issue in fixture.expected.issues
        if _severity_rank(expected_issue.severity.value) <= _severity_rank(severity)
    )


def _location_matches(pattern: str, location: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, location) is not None
    except re.error:
        return pattern in location


def _semantic_location_matches(expected_issue: Any, location: str) -> bool:
    expected_path = (
        str(getattr(expected_issue, "path", "") or "").strip().replace("\\", "/")
    )
    raw_expected_line = getattr(expected_issue, "line", None)
    raw_expected_end_line = getattr(expected_issue, "end_line", None)
    expected_line = raw_expected_line if isinstance(raw_expected_line, int) else None
    expected_end_line = (
        raw_expected_end_line if isinstance(raw_expected_end_line, int) else None
    )
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


def _issue_matches_expected_location(expected_issue: Any, issue: Any) -> bool:
    location = str(getattr(issue, "location", "") or "")
    location_matches = (
        _semantic_location_matches(expected_issue, location)
        or _location_matches(expected_issue.location_pattern, location)
        or _issue_evidence_mentions_expected_line(expected_issue, issue)
    )
    if not location_matches:
        return False
    if not _semantic_text_matches(
        str(getattr(expected_issue, "mechanism_pattern", "") or ""),
        str(getattr(issue, "causal_mechanism", "") or ""),
    ):
        return False
    if not _semantic_text_matches(
        str(getattr(expected_issue, "invariant_pattern", "") or ""),
        str(getattr(issue, "violated_invariant", "") or ""),
    ):
        return False
    expected_paths = {
        str(path).strip().replace("\\", "/")
        for path in getattr(expected_issue, "affected_paths", [])
        if str(path).strip()
    }
    if not expected_paths:
        return True
    actual_paths = {normalize_location(location).path}
    actual_paths.update(
        str(getattr(item, "file", "") or "").strip().replace("\\", "/")
        for item in getattr(issue, "related_locations", [])
    )
    return expected_paths.issubset(actual_paths)


def _semantic_text_matches(pattern: str, value: str) -> bool:
    if not pattern:
        return True
    try:
        if re.search(pattern, value, re.IGNORECASE) is not None:
            return True
    except re.error:
        pass
    normalized_value = _semantic_match_tokens(value)
    alternatives = [item for item in pattern.split("|") if item.strip()]
    return any(
        _semantic_match_tokens(alternative).issubset(normalized_value)
        for alternative in alternatives
    )


def _semantic_match_tokens(value: str) -> set[str]:
    aliases = {
        "single": "once",
        "one": "once",
        "double": "duplicate",
        "duplicate": "duplicate",
        "redundant": "duplicate",
        "revert": "remove",
        "second": "duplicate",
        "twice": "duplicate",
        "application": "apply",
        "applied": "apply",
        "applies": "apply",
        "subtracting": "subtract",
        "subtraction": "subtract",
        "subtracts": "subtract",
    }
    words = re.findall(r"[a-z0-9]+", value.lower().replace("_", " "))
    return {aliases.get(word, word) for word in words}


def _default_eval_variant() -> EvalVariant:
    mode = get_settings().review_context_mode
    return EvalVariant(
        id="default-agent-search" if mode == "agent_search" else "default-graph-hybrid",
        context_mode=mode,
        graph_cache_mode="disabled" if mode == "agent_search" else "warm",
    )


def _variant_result_fields(variant: EvalVariant) -> dict[str, str]:
    return {
        "variant_id": variant.id,
        "context_mode": variant.context_mode,
        "graph_cache_mode": variant.graph_cache_mode,
        "matcher_version": EVAL_MATCHER_VERSION,
    }


def _issue_evidence_mentions_expected_line(expected_issue: Any, issue: Any) -> bool:
    expected_path = (
        str(getattr(expected_issue, "path", "") or "").strip().replace("\\", "/")
    )
    expected_line = getattr(expected_issue, "line", None)
    if not expected_path or not isinstance(expected_line, int):
        return False
    location = str(getattr(issue, "location", "") or "")
    parsed = normalize_location(location)
    if not parsed.valid or parsed.path != expected_path:
        return False
    evidence = str(getattr(issue, "evidence", "") or "")
    expected_end = getattr(expected_issue, "end_line", None)
    lines = range(
        expected_line,
        (expected_end if isinstance(expected_end, int) else expected_line) + 1,
    )
    return any(_evidence_mentions_line(evidence, line) for line in lines)


def _evidence_mentions_line(evidence: str, line: int) -> bool:
    if not evidence:
        return False
    return (
        re.search(rf"\bline\s+{line}\b|:{line}\b", evidence, re.IGNORECASE) is not None
    )


def _is_eval_effective_issue(issue: Any, fixture: Fixture | None = None) -> bool:
    severity = str(
        getattr(getattr(issue, "severity", ""), "value", getattr(issue, "severity", ""))
    )
    confidence_raw = getattr(issue, "confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = str(getattr(issue, "evidence", "") or "")

    if severity == Severity.CRITICAL.value:
        return confidence >= _MIN_CRITICAL_CONFIDENCE and (
            has_specific_diff_evidence(evidence)
            or has_specific_code_evidence(evidence)
            or _issue_location_is_changed_line(issue, fixture)
        )
    if severity == Severity.WARNING.value:
        return confidence >= _MIN_WARNING_CONFIDENCE and (
            has_specific_diff_evidence(evidence)
            or _issue_location_is_changed_line(issue, fixture)
        )
    return False


def _issue_location_is_changed_line(issue: Any, fixture: Fixture | None) -> bool:
    if (
        fixture is None
        or fixture.type != "review"
        or not fixture.input.diff_text.strip()
    ):
        return False
    location = str(getattr(issue, "location", "") or "")
    parsed = normalize_location(location)
    if not parsed.valid or not parsed.path or parsed.line is None:
        return False
    changed_for_path = _changed_new_lines_by_file(fixture.input.diff_text).get(
        parsed.path,
        set(),
    )
    if not changed_for_path:
        return False
    actual_start = parsed.line
    actual_end = parsed.end_line or actual_start
    return any(line in changed_for_path for line in range(actual_start, actual_end + 1))
