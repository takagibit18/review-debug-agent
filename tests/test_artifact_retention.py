"""Artifact retention tests that never rely on filesystem modification times."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from cli import main
from src.platform.artifacts import ArtifactStore
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository
from src.platform.retention import cleanup_artifacts


def test_cleanup_uses_db_time_and_preserves_active_runs_and_history(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    with connect(database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        installation, tracked_repository = _seed_tenant(repository)
        old_run = _seed_run_with_artifacts(
            repository,
            artifact_store,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-old-completed",
            terminal=True,
        )
        new_run = _seed_run_with_artifacts(
            repository,
            artifact_store,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-new-completed",
            terminal=True,
        )
        running_run = _seed_run_with_artifacts(
            repository,
            artifact_store,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-old-running",
            terminal=False,
        )
        repository.create_usage_record(
            run_id=old_run.run_id,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            model_name="fake-model",
            total_tokens=12,
            attempt=1,
        )
        repository.insert_webhook_delivery(
            delivery_id="delivery-retention",
            installation_id=installation.id,
            event="pull_request",
            action="opened",
            repo_full_name=tracked_repository.full_name,
            pr_number=7,
            head_sha="head-old",
        )
        _set_run_time(conn, old_run.run_id, now - timedelta(days=60))
        _set_run_time(conn, new_run.run_id, now - timedelta(days=5))
        _set_run_time(conn, running_run.run_id, now - timedelta(days=90))

        old_path_before = repository.get_run(old_run.run_id)
        old_checkpoint_before = repository.list_checkpoints(old_run.run_id)[0]
        dry_run = cleanup_artifacts(
            repository,
            artifact_store,
            retention_days=30,
            dry_run=True,
            now=now,
        )

        assert dry_run.eligible_run_ids == (old_run.run_id,)
        assert dry_run.cleaned_run_ids == ()
        assert (artifact_store.root / old_run.run_id).is_dir()
        assert repository.get_run(old_run.run_id) == old_path_before
        assert repository.list_checkpoints(old_run.run_id)[0] == old_checkpoint_before

        result = cleanup_artifacts(
            repository,
            artifact_store,
            retention_days=30,
            now=now,
        )

        assert result.eligible_run_ids == (old_run.run_id,)
        assert result.cleaned_run_ids == (old_run.run_id,)
        assert result.deleted_directory_count == 1
        assert not (artifact_store.root / old_run.run_id).exists()
        assert (artifact_store.root / new_run.run_id).is_dir()
        assert (artifact_store.root / running_run.run_id).is_dir()

        retained_old = repository.get_run(old_run.run_id)
        retained_new = repository.get_run(new_run.run_id)
        retained_running = repository.get_run(running_run.run_id)
        assert retained_old is not None
        assert retained_old.status == "succeeded"
        assert retained_old.review_response_path == ""
        assert retained_old.run_summary_path == ""
        assert retained_old.publish_result_path == ""
        assert repository.list_checkpoints(old_run.run_id)[0].output_artifact_path == ""
        assert retained_new is not None and retained_new.review_response_path
        assert retained_running is not None and retained_running.status == "running"
        assert retained_running.review_response_path == ""
        assert repository.list_checkpoints(running_run.run_id)[0].output_artifact_path

        assert len(repository.list_runs()) == 3
        assert repository.list_usage_records(run_id=old_run.run_id)[0].total_tokens == 12
        assert repository.list_deliveries(installation_id=installation.id)[0].delivery_id == (
            "delivery-retention"
        )
        assert repository.get_installation(installation.id) is not None
        assert repository.get_repository(
            installation_id=installation.id,
            full_name=tracked_repository.full_name,
        ) is not None


def test_cleanup_cli_supports_dry_run_and_retention_override(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("PLATFORM_ARTIFACT_RETENTION_DAYS", "1")

    with connect(database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        installation, tracked_repository = _seed_tenant(repository)
        old_run = _seed_run_with_artifacts(
            repository,
            ArtifactStore(artifact_root),
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-cli-old",
            terminal=True,
        )
        _set_run_time(conn, old_run.run_id, datetime(2000, 1, 1, tzinfo=UTC))

    runner = CliRunner()
    preview = runner.invoke(
        main,
        ["platform", "cleanup", "--dry-run", "--retention-days", "30"],
    )
    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.output)
    assert preview_payload["dry_run"] is True
    assert preview_payload["retention_days"] == 30
    assert preview_payload["eligible_run_ids"] == [old_run.run_id]
    assert (artifact_root / old_run.run_id).is_dir()

    cleanup = runner.invoke(main, ["platform", "cleanup", "--retention-days", "30"])
    assert cleanup.exit_code == 0, cleanup.output
    cleanup_payload = json.loads(cleanup.output)
    assert cleanup_payload["cleaned_run_ids"] == [old_run.run_id]
    assert cleanup_payload["deleted_directory_count"] == 1
    assert not (artifact_root / old_run.run_id).exists()

    with connect(database_url) as conn:
        retained = PlatformRepository(conn).get_run(old_run.run_id)
        assert retained is not None
        assert retained.status == "succeeded"
        assert retained.review_response_path == ""


def test_artifact_store_refuses_unsafe_cleanup_targets(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    for run_id in ("", ".", "..", "../outside", "nested/run"):
        try:
            store.delete_run_artifacts(run_id)
        except ValueError:
            continue
        raise AssertionError(f"unsafe run id was accepted: {run_id!r}")


def _seed_tenant(repository: PlatformRepository):  # type: ignore[no-untyped-def]
    installation = repository.upsert_installation(
        github_installation_id=123,
        account_login="owner",
        account_type="User",
    )
    tracked_repository = repository.upsert_repository(
        installation_id=installation.id,
        full_name="owner/repo",
        owner="owner",
        name="repo",
        default_branch="main",
    )
    return installation, tracked_repository


def _seed_run_with_artifacts(
    repository: PlatformRepository,
    artifact_store: ArtifactStore,
    *,
    installation_id: int,
    repository_id: int,
    run_id: str,
    terminal: bool,
):  # type: ignore[no-untyped-def]
    repository.create_review_run(
        installation_id=installation_id,
        repository_id=repository_id,
        repo_full_name="owner/repo",
        pr_number=7,
        head_sha=f"{run_id}-head",
        base_sha="base",
        trigger_event="pull_request",
        trigger_action="opened",
        run_id=run_id,
    )
    run = repository.claim_next_queued_run(worker_id="test-worker", lease_seconds=180)
    assert run is not None and run.run_id == run_id
    repository.start_checkpoint(run_id, "review", attempt=run.attempt)
    pipeline_path = artifact_store.save_pipeline_result(run_id, {"run_id": run_id})
    repository.complete_checkpoint(
        run_id,
        "review",
        attempt=run.attempt,
        output_artifact_path=pipeline_path,
    )
    paths = artifact_store.save_review_artifacts(
        run_id,
        SimpleNamespace(
            review_response={"run_id": run_id},
            run_summary={"total_tokens": 1},
            publish_result={"status": "published"},
            diff_text="diff --git a/a.py b/a.py\n",
            changed_lines={"a.py": [1]},
            event_log_paths=[],
        ),
    )
    if terminal:
        return repository.mark_run_succeeded(run_id, **paths)
    return run


def _set_run_time(conn, run_id: str, timestamp: datetime) -> None:  # type: ignore[no-untyped-def]
    value = timestamp.astimezone(UTC).isoformat()
    conn.execute(
        """
        UPDATE review_runs
        SET created_at = ?, updated_at = ?, finished_at = CASE
            WHEN status IN ('succeeded', 'failed', 'cancelled', 'skipped') THEN ?
            ELSE NULL
        END
        WHERE run_id = ?
        """,
        (value, value, value, run_id),
    )
    conn.commit()
