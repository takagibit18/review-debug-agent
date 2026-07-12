"""Tests for durable run leases, checkpoints, and stale-run recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository


def _seed_run(repo: PlatformRepository) -> str:
    installation = repo.upsert_installation(
        github_installation_id=501,
        account_login="owner",
        account_type="User",
    )
    repository = repo.upsert_repository(
        installation_id=installation.id,
        full_name="owner/recovery",
        owner="owner",
        name="recovery",
        default_branch="main",
    )
    return repo.create_review_run(
        installation_id=installation.id,
        repository_id=repository.id,
        repo_full_name="owner/recovery",
        pr_number=9,
        head_sha="head-9",
        base_sha="base-9",
        trigger_event="pull_request",
        trigger_action="opened",
    ).run_id


def test_claim_lease_is_atomic_and_owner_can_heartbeat(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    with connect(database_url) as first_conn, connect(database_url) as second_conn:
        init_db(first_conn)
        init_db(second_conn)
        first = PlatformRepository(first_conn)
        second = PlatformRepository(second_conn)
        run_id = _seed_run(first)

        claimed = first.claim_next_queued_run(worker_id="worker-1", lease_seconds=60)
        duplicate = second.claim_next_queued_run(worker_id="worker-2", lease_seconds=60)

        assert claimed is not None
        assert claimed.run_id == run_id
        assert claimed.lease_owner == "worker-1"
        assert claimed.attempt == 1
        assert duplicate is None
        assert second.heartbeat_run(run_id, "worker-2", lease_seconds=60) is False
        assert first.heartbeat_run(run_id, "worker-1", lease_seconds=60) is True


def test_expired_run_requeues_and_resumes_from_first_incomplete_step(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    with connect(database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_run(repo)
        repo.claim_next_queued_run(worker_id="dead-worker", lease_seconds=60)
        repo.start_checkpoint(run_id, "fetch_pr_context", attempt=1, input_digest="a")
        repo.complete_checkpoint(run_id, "fetch_pr_context", attempt=1, output_artifact_path="a.json")
        repo.start_checkpoint(run_id, "build_review_context", attempt=1, input_digest="b")
        conn.execute(
            "UPDATE review_runs SET lease_expires_at = ? WHERE run_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), run_id),
        )
        conn.commit()

        requeued = repo.requeue_expired_runs(
            datetime.now(UTC),
            ordered_steps=(
                "fetch_pr_context",
                "build_review_context",
                "analyze_candidates",
            ),
        )
        run = repo.get_run(run_id)

        assert requeued == [run_id]
        assert run is not None
        assert run.status == "queued"
        assert run.lease_owner == ""
        assert run.resume_from_step == "build_review_context"
        assert run.attempt == 1


def test_checkpoint_transitions_are_persisted_and_idempotent(tmp_path: Path) -> None:
    with connect(f"sqlite:///{tmp_path / 'platform.db'}") as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_run(repo)

        started = repo.start_checkpoint(
            run_id,
            "verify_findings",
            attempt=1,
            input_digest="digest-1",
        )
        repeated = repo.start_checkpoint(
            run_id,
            "verify_findings",
            attempt=1,
            input_digest="digest-1",
        )
        completed = repo.complete_checkpoint(
            run_id,
            "verify_findings",
            attempt=1,
            output_artifact_path="verify.json",
        )
        checkpoints = repo.list_checkpoints(run_id)

        assert started.id == repeated.id
        assert completed.status == "completed"
        assert completed.output_artifact_path == "verify.json"
        assert len(checkpoints) == 1


def test_init_db_upgrades_existing_review_runs_without_data_loss(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    with connect(database_url) as conn:
        conn.executescript(
            """
            CREATE TABLE review_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                installation_id INTEGER NOT NULL,
                repository_id INTEGER NOT NULL,
                repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                base_sha TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                trigger_event TEXT NOT NULL DEFAULT '',
                trigger_action TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                review_response_path TEXT NOT NULL DEFAULT '',
                run_summary_path TEXT NOT NULL DEFAULT '',
                publish_result_path TEXT NOT NULL DEFAULT '',
                total_tokens INTEGER,
                publish_status TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO review_runs(
                run_id, installation_id, repository_id, status,
                repo_full_name, pr_number, head_sha
            )
            VALUES ('legacy-run', 1, 1, 'failed', 'owner/repo', 1, 'abc');
            """
        )
        conn.commit()

        init_db(conn)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(review_runs)").fetchall()
        }
        legacy = conn.execute(
            "SELECT run_id, status FROM review_runs WHERE run_id = 'legacy-run'"
        ).fetchone()

        assert {
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "resume_from_step",
            "attempt",
        } <= columns
        assert legacy["status"] == "failed"


def test_worker_records_pipeline_and_artifact_checkpoints(tmp_path: Path, monkeypatch) -> None:
    from src.config import get_settings
    from src.platform.worker import PlatformWorker, ReviewPipelineResult

    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'platform.db'}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RUN_CHECKPOINTS_ENABLED", "true")
    monkeypatch.setenv("RUN_LEASE_SECONDS", "60")
    monkeypatch.setenv("RUN_HEARTBEAT_SECONDS", "1")
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_run(repo)

        async def pipeline(run, config):  # type: ignore[no-untyped-def]
            assert run.lease_owner == "worker-test"
            return ReviewPipelineResult(
                review_response={"run_id": run.run_id, "report": {"summary": "ok", "issues": []}},
                publish_result={"status": "published"},
                run_summary={"run_id": run.run_id, "total_tokens": 7},
                total_tokens=7,
            )

        processed = PlatformWorker(
            repo,
            settings=settings,
            pipeline=pipeline,
            worker_id="worker-test",
        ).run_once()
        run = repo.get_run(run_id)
        checkpoints = repo.list_checkpoints(run_id)

    assert processed is True
    assert run is not None
    assert run.status == "succeeded"
    assert run.lease_owner == ""
    assert [(item.step_id, item.status) for item in checkpoints] == [
        ("review_pipeline", "completed"),
        ("persist_artifacts", "completed"),
    ]


def test_worker_resume_uses_verified_pipeline_artifact_without_model_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    from src.config import get_settings
    from src.platform.artifacts import ArtifactStore
    from src.platform.worker import PlatformWorker, ReviewPipelineResult

    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'platform.db'}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RUN_CHECKPOINTS_ENABLED", "true")
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_run(repo)
        repo.claim_next_queued_run(worker_id="dead", lease_seconds=60)
        store = ArtifactStore(settings.platform_artifact_root)
        cached = ReviewPipelineResult(
            review_response={"run_id": run_id},
            run_summary={"total_tokens": 3},
            total_tokens=3,
        )
        artifact_path = store.save_pipeline_result(run_id, cached)
        repo.start_checkpoint(run_id, "review_pipeline", attempt=1)
        repo.complete_checkpoint(
            run_id, "review_pipeline", attempt=1,
            output_artifact_path=artifact_path,
        )
        conn.execute(
            "UPDATE review_runs SET lease_expires_at = ? WHERE run_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), run_id),
        )
        conn.commit()
        calls = 0

        async def pipeline(run, config):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise AssertionError("completed pipeline must not execute again")

        assert PlatformWorker(
            repo, settings=settings, pipeline=pipeline, artifact_store=store,
            worker_id="recovery",
        ).run_once() is True
        recovered = repo.get_run(run_id)

    assert calls == 0
    assert recovered is not None and recovered.status == "succeeded"
    assert recovered.attempt == 2


def test_usage_record_is_idempotent_per_run_attempt(tmp_path: Path) -> None:
    with connect(f"sqlite:///{tmp_path / 'platform.db'}") as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_run(repo)
        run = repo.get_run(run_id)
        assert run is not None

        first = repo.create_usage_record(
            run_id=run_id,
            installation_id=run.installation_id,
            repository_id=run.repository_id,
            model_name="model",
            total_tokens=10,
            attempt=1,
        )
        repeated = repo.create_usage_record(
            run_id=run_id,
            installation_id=run.installation_id,
            repository_id=run.repository_id,
            model_name="model",
            total_tokens=10,
            attempt=1,
        )

        assert first.id == repeated.id
        assert repeated.attempt == 1
        assert len(repo.list_usage_records(run_id=run_id)) == 1


def test_artifact_store_writes_atomically_without_temp_files(tmp_path: Path) -> None:
    from src.platform.artifacts import ArtifactStore
    from src.platform.worker import ReviewPipelineResult

    store = ArtifactStore(tmp_path / "artifacts")
    result = ReviewPipelineResult(review_response={"run_id": "run-a", "value": 1})

    first = store.save_review_artifacts("run-a", result)
    result.review_response = {"run_id": "run-a", "value": 2}
    second = store.save_review_artifacts("run-a", result)

    artifact = tmp_path / "artifacts" / first["review_response_path"]
    digest_path = artifact.with_name(artifact.name + ".sha256")
    assert first == second
    assert '"value": 2' in artifact.read_text(encoding="utf-8")
    assert list(artifact.parent.glob("*.tmp")) == []
    assert digest_path.read_text(encoding="ascii").strip() == hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
