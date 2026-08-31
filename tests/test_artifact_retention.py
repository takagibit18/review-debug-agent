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
    event_log_root = tmp_path / "event-logs"
    old_event_log = event_log_root / "agent-old-completed.jsonl"
    new_event_log = event_log_root / "agent-new-completed.jsonl"
    running_event_log = event_log_root / "agent-old-running.jsonl"
    event_log_root.mkdir()
    old_event_log.write_text('{"run_id":"old"}\n', encoding="utf-8")
    new_event_log.write_text('{"run_id":"new"}\n', encoding="utf-8")
    running_event_log.write_text('{"run_id":"running"}\n', encoding="utf-8")
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
            event_log_paths=[str(old_event_log)],
        )
        new_run = _seed_run_with_artifacts(
            repository,
            artifact_store,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-new-completed",
            terminal=True,
            event_log_paths=[str(new_event_log)],
        )
        running_run = _seed_run_with_artifacts(
            repository,
            artifact_store,
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-old-running",
            terminal=False,
            event_log_paths=[str(running_event_log)],
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
            event_log_dir=event_log_root,
        )

        assert dry_run.eligible_run_ids == (old_run.run_id,)
        assert dry_run.cleaned_run_ids == ()
        assert (artifact_store.root / old_run.run_id).is_dir()
        assert old_event_log.exists()
        assert repository.get_run(old_run.run_id) == old_path_before
        assert repository.list_checkpoints(old_run.run_id)[0] == old_checkpoint_before

        result = cleanup_artifacts(
            repository,
            artifact_store,
            retention_days=30,
            now=now,
            event_log_dir=event_log_root,
        )

        assert result.eligible_run_ids == (old_run.run_id,)
        assert result.cleaned_run_ids == (old_run.run_id,)
        assert result.deleted_directory_count == 1
        assert result.deleted_event_log_count == 1
        assert not (artifact_store.root / old_run.run_id).exists()
        assert not old_event_log.exists()
        assert (artifact_store.root / new_run.run_id).is_dir()
        assert (artifact_store.root / running_run.run_id).is_dir()
        assert new_event_log.exists()
        assert running_event_log.exists()

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

        repeated = cleanup_artifacts(
            repository,
            artifact_store,
            retention_days=30,
            now=now,
            event_log_dir=event_log_root,
        )
        assert repeated.cleaned_run_ids == (old_run.run_id,)
        assert repeated.deleted_directory_count == 0
        assert repeated.deleted_event_log_count == 0


def test_cleanup_cli_supports_dry_run_and_retention_override(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    artifact_root = tmp_path / "artifacts"
    event_log_root = tmp_path / "event-logs"
    cli_event_log = event_log_root / "agent-cli-old.jsonl"
    event_log_root.mkdir()
    cli_event_log.write_text('{"run_id":"cli-old"}\n', encoding="utf-8")
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url)
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("EVENT_LOG_DIR", str(event_log_root))
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
            event_log_paths=[str(cli_event_log)],
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
    assert cli_event_log.exists()

    cleanup = runner.invoke(main, ["platform", "cleanup", "--retention-days", "30"])
    assert cleanup.exit_code == 0, cleanup.output
    cleanup_payload = json.loads(cleanup.output)
    assert cleanup_payload["cleaned_run_ids"] == [old_run.run_id]
    assert cleanup_payload["deleted_directory_count"] == 1
    assert not (artifact_root / old_run.run_id).exists()
    assert not cli_event_log.exists()

    with connect(database_url) as conn:
        retained = PlatformRepository(conn).get_run(old_run.run_id)
        assert retained is not None
        assert retained.status == "succeeded"
        assert retained.review_response_path == ""


def test_cleanup_handles_missing_owned_event_log_idempotently(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    artifact_root = tmp_path / "artifacts"
    event_log_root = tmp_path / "event-logs"
    event_log_root.mkdir()
    missing_event_log = event_log_root / "already-removed.jsonl"
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    with connect(database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        installation, tracked_repository = _seed_tenant(repository)
        old_run = _seed_run_with_artifacts(
            repository,
            ArtifactStore(artifact_root),
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-missing-event-log",
            terminal=True,
            event_log_paths=[str(missing_event_log)],
        )
        _set_run_time(conn, old_run.run_id, now - timedelta(days=60))

        result = cleanup_artifacts(
            repository,
            ArtifactStore(artifact_root),
            retention_days=30,
            now=now,
            event_log_dir=event_log_root,
        )

        assert result.cleaned_run_ids == (old_run.run_id,)
        assert result.deleted_event_log_count == 0
        assert not (artifact_root / old_run.run_id).exists()
        retained = repository.get_run(old_run.run_id)
        assert retained is not None
        assert retained.review_response_path == ""


def test_cleanup_skips_unsafe_owned_event_log_paths(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    artifact_root = tmp_path / "artifacts"
    event_log_root = tmp_path / "event-logs"
    event_log_root.mkdir()
    safe_event_log = event_log_root / "safe.jsonl"
    outside_event_log = tmp_path / "outside.jsonl"
    safe_event_log.write_text('{"safe":true}\n', encoding="utf-8")
    outside_event_log.write_text('{"outside":true}\n', encoding="utf-8")
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    with connect(database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        installation, tracked_repository = _seed_tenant(repository)
        old_run = _seed_run_with_artifacts(
            repository,
            ArtifactStore(artifact_root),
            installation_id=installation.id,
            repository_id=tracked_repository.id,
            run_id="run-unsafe-event-log",
            terminal=True,
            event_log_paths=[str(safe_event_log), str(outside_event_log)],
        )
        _set_run_time(conn, old_run.run_id, now - timedelta(days=60))

        result = cleanup_artifacts(
            repository,
            ArtifactStore(artifact_root),
            retention_days=30,
            now=now,
            event_log_dir=event_log_root,
        )

        assert result.deleted_event_log_count == 1
        assert result.skipped_unsafe_event_log_paths == (str(outside_event_log),)
        assert not safe_event_log.exists()
        assert outside_event_log.exists()


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
    event_log_paths: list[str] | None = None,
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
    repository.start_checkpoint(run_id, "review_pipeline", attempt=run.attempt)
    pipeline_path = artifact_store.save_pipeline_result(
        run_id,
        {"run_id": run_id, "event_log_paths": event_log_paths or []},
    )
    repository.complete_checkpoint(
        run_id,
        "review_pipeline",
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
            event_log_paths=event_log_paths or [],
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
