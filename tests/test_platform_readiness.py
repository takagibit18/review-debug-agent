"""Offline liveness/readiness coverage for the DB-backed worker service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import app
from src.config import get_settings
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository
from src.platform.worker import PlatformWorker, worker_stale_after_seconds


def test_ready_reports_missing_worker_with_accessible_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unready",
        "database": "ok",
        "worker": "missing",
        "queue_depth": 0,
        "worker_stale_after_seconds": 30,
    }


def test_ready_reports_fresh_worker_and_queue_depth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        repository.upsert_worker_heartbeat(
            "fresh-worker",
            seen_at=datetime.now(UTC),
        )
        _seed_queued_run(repository)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "worker": "healthy",
        "queue_depth": 1,
        "worker_stale_after_seconds": 30,
    }


def test_ready_rejects_stale_worker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = get_settings()
    stale_after = worker_stale_after_seconds(settings)
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        repository.upsert_worker_heartbeat(
            "stale-worker",
            seen_at=datetime.now(UTC) - timedelta(seconds=stale_after + 5),
        )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "ok"
    assert response.json()["worker"] == "stale"
    assert response.json()["queue_depth"] == 0


def test_ready_reports_database_unavailable_without_exposing_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file blocks database parent", encoding="utf-8")
    monkeypatch.setenv(
        "PLATFORM_DATABASE_URL",
        f"sqlite:///{blocked_parent / 'platform.db'}",
    )
    monkeypatch.setenv("PLATFORM_INIT_DB_ON_STARTUP", "false")
    monkeypatch.setenv("PLATFORM_PUBLIC_GITHUB_APP_ONLY", "false")

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert response.json()["worker"] == "unknown"
    assert response.json()["queue_depth"] is None
    assert str(blocked_parent) not in response.text


def test_idle_worker_poll_records_service_heartbeat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        worker = PlatformWorker(
            repository,
            settings=settings,
            worker_id="idle-worker",
        )

        assert worker.run_once() is False
        heartbeat = repository.freshest_worker_heartbeat()

    assert heartbeat is not None
    assert heartbeat.worker_id == "idle-worker"
    assert heartbeat.started_at == heartbeat.last_seen_at


def _configure(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(
        "PLATFORM_DATABASE_URL",
        f"sqlite:///{tmp_path / 'platform.db'}",
    )
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLATFORM_PUBLIC_GITHUB_APP_ONLY", "false")
    monkeypatch.setenv("PLATFORM_WORKER_POLL_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("RUN_HEARTBEAT_SECONDS", "2")


def _seed_queued_run(repository: PlatformRepository) -> None:
    installation = repository.upsert_installation(
        github_installation_id=7001,
        account_login="ready",
        account_type="Organization",
    )
    repo = repository.upsert_repository(
        installation_id=installation.id,
        full_name="ready/repo",
        owner="ready",
        name="repo",
        default_branch="main",
    )
    repository.create_review_run(
        installation_id=installation.id,
        repository_id=repo.id,
        repo_full_name=repo.full_name,
        pr_number=1,
        head_sha="a" * 40,
        base_sha="b" * 40,
        trigger_event="pull_request",
        trigger_action="opened",
    )
