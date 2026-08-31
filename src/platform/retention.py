"""Database-driven retention for local platform run artifacts."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.platform.artifacts import ArtifactStore
from src.platform.repositories import PlatformRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactCleanupResult:
    """Summary of one deterministic artifact cleanup pass."""

    dry_run: bool
    retention_days: int
    cutoff: str
    eligible_run_ids: tuple[str, ...]
    cleaned_run_ids: tuple[str, ...]
    deleted_directory_count: int
    deleted_event_log_count: int = 0
    skipped_unsafe_event_log_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cleanup_artifacts(
    repository: PlatformRepository,
    artifact_store: ArtifactStore,
    *,
    retention_days: int,
    dry_run: bool = False,
    now: datetime | None = None,
    event_log_dir: str | Path | None = None,
) -> ArtifactCleanupResult:
    """Clean old terminal-run artifacts and their owned source event logs."""
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current_time - timedelta(days=retention_days)
    candidates = repository.list_runs_for_artifact_cleanup(
        completed_before=cutoff,
    )
    eligible_run_ids = tuple(run.run_id for run in candidates)
    if dry_run:
        return ArtifactCleanupResult(
            dry_run=True,
            retention_days=retention_days,
            cutoff=cutoff.isoformat(),
            eligible_run_ids=eligible_run_ids,
            cleaned_run_ids=(),
            deleted_directory_count=0,
        )

    cleaned_run_ids: list[str] = []
    deleted_directory_count = 0
    deleted_event_log_count = 0
    skipped_unsafe_event_log_paths: list[str] = []
    skipped_path_set: set[str] = set()
    for run in candidates:
        event_log_paths = _event_log_paths_for_run(repository, artifact_store, run)
        if event_log_dir is not None:
            for raw_path in event_log_paths:
                source, reason = _safe_event_log_source(event_log_dir, raw_path)
                if source is None:
                    if raw_path not in skipped_path_set:
                        skipped_path_set.add(raw_path)
                        skipped_unsafe_event_log_paths.append(raw_path)
                    logger.warning(
                        "skipping unsafe source event log during retention cleanup",
                        extra={
                            "run_id": run.run_id,
                            "event_log_path": raw_path,
                            "reason": reason,
                        },
                    )
                    continue
                if not source.exists():
                    continue
                if not source.is_file() or source.is_symlink():
                    if raw_path not in skipped_path_set:
                        skipped_path_set.add(raw_path)
                        skipped_unsafe_event_log_paths.append(raw_path)
                    logger.warning(
                        "skipping non-regular source event log during retention cleanup",
                        extra={
                            "run_id": run.run_id,
                            "event_log_path": raw_path,
                        },
                    )
                    continue
                try:
                    source.unlink()
                except FileNotFoundError:
                    # A source log disappearing between the safety check and unlink
                    # is an idempotent no-op.
                    continue
                deleted_event_log_count += 1
        if artifact_store.delete_run_artifacts(run.run_id):
            deleted_directory_count += 1
        repository.clear_run_artifact_paths(run.run_id)
        cleaned_run_ids.append(run.run_id)

    return ArtifactCleanupResult(
        dry_run=False,
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        eligible_run_ids=eligible_run_ids,
        cleaned_run_ids=tuple(cleaned_run_ids),
        deleted_directory_count=deleted_directory_count,
        deleted_event_log_count=deleted_event_log_count,
        skipped_unsafe_event_log_paths=tuple(skipped_unsafe_event_log_paths),
    )


def _event_log_paths_for_run(
    repository: PlatformRepository,
    artifact_store: ArtifactStore,
    run: Any,
) -> tuple[str, ...]:
    """Read source-log ownership from durable artifacts before deleting them."""
    paths: list[str] = []
    seen: set[str] = set()
    for checkpoint in repository.list_checkpoints(run.run_id):
        if checkpoint.step_id != "review_pipeline" or not checkpoint.output_artifact_path:
            continue
        try:
            persisted_paths = artifact_store.load_event_log_paths(
                checkpoint.output_artifact_path
            )
        except FileNotFoundError:
            # The artifact directory may already have been removed by a prior pass.
            continue
        for path in persisted_paths:
            if path not in seen:
                seen.add(path)
                paths.append(path)

    if paths or not run.run_summary_path:
        return tuple(paths)

    try:
        summary = artifact_store.load_pipeline_result(run.run_summary_path)
    except FileNotFoundError:
        return ()
    event_log = summary.get("event_log")
    if isinstance(event_log, dict):
        path = event_log.get("event_log_path")
        if isinstance(path, str) and path.strip():
            return (path,)
    return ()


def _safe_event_log_source(
    event_log_dir: str | Path,
    raw_path: str,
) -> tuple[Path | None, str]:
    """Resolve one source log only when it is safely owned by event_log_dir."""
    raw = raw_path.strip()
    if not raw:
        return None, "empty path"

    path = Path(raw)
    if ".." in path.parts:
        return None, "path traversal"

    root = Path(event_log_dir).resolve()
    candidate = path if path.is_absolute() else root / path
    candidate = candidate.absolute()
    try:
        candidate_relative = candidate.relative_to(root)
    except ValueError:
        return None, "outside configured event log directory"

    cursor = root
    for part in candidate_relative.parts:
        cursor /= part
        if cursor.is_symlink():
            return None, "symlink path component"

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return None, f"path resolution failed: {exc.__class__.__name__}"
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, "symlink escapes configured event log directory"
    if not relative.parts:
        return None, "event log directory is not a file"

    return resolved, ""
