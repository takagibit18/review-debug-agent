"""Database-driven retention for local platform run artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.platform.artifacts import ArtifactStore
from src.platform.repositories import PlatformRepository


@dataclass(frozen=True)
class ArtifactCleanupResult:
    """Summary of one deterministic artifact cleanup pass."""

    dry_run: bool
    retention_days: int
    cutoff: str
    eligible_run_ids: tuple[str, ...]
    cleaned_run_ids: tuple[str, ...]
    deleted_directory_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cleanup_artifacts(
    repository: PlatformRepository,
    artifact_store: ArtifactStore,
    *,
    retention_days: int,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ArtifactCleanupResult:
    """Clean artifacts for old terminal runs while retaining all DB history."""
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
    for run in candidates:
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
    )
