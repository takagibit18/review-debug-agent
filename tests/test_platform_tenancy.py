"""Tenant context and isolation tests for the platform management APIs."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


def test_tenant_status_resolves_from_header(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.api.app import app
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        tenant = _seed_tenant(repo, github_installation_id=101, account_login="tenant-a")

    response = TestClient(app).get(
        "/platform/tenant",
        headers={"X-MergeWarden-Tenant": str(tenant.id)},
    )

    assert response.status_code == 200
    assert response.json() == {
        "resolved": True,
        "source": "header",
        "header_name": "X-MergeWarden-Tenant",
        "tenant": {
            "id": tenant.id,
            "name": "tenant-a",
            "github_installation_id": 101,
            "account_login": "tenant-a",
            "account_type": "Organization",
            "status": "active",
        },
    }


def test_invalid_tenant_header_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.api.app import app

    _configure_platform(monkeypatch, tmp_path)

    response = TestClient(app).get(
        "/platform/tenant",
        headers={"X-MergeWarden-Tenant": "not-a-tenant"},
    )

    assert response.status_code == 400
    assert response.json() == {"message": "invalid tenant id", "run_id": ""}


def test_missing_tenant_is_rejected_for_core_platform_reads(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.api.app import app

    _configure_platform(monkeypatch, tmp_path)

    response = TestClient(app).get("/platform/runs")

    assert response.status_code == 400
    assert response.json() == {"message": "tenant required", "run_id": ""}


def test_default_tenant_allows_dev_requests_without_header(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.api.app import app
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        tenant = _seed_tenant(repo, github_installation_id=101, account_login="tenant-a")
        run_id = _seed_run(repo, tenant.id, repo_name="tenant-a/repo", run_id="run-a")
    monkeypatch.setenv("PLATFORM_DEFAULT_TENANT_ID", str(tenant.id))

    client = TestClient(app)
    status = client.get("/platform/tenant")
    runs = client.get("/platform/runs")

    assert status.status_code == 200
    assert status.json()["resolved"] is True
    assert status.json()["source"] == "default"
    assert runs.status_code == 200
    assert [item["run_id"] for item in runs.json()] == [run_id]


def test_tenant_scoped_runs_do_not_cross_installations(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.api.app import app
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        tenant_a = _seed_tenant(repo, github_installation_id=101, account_login="tenant-a")
        tenant_b = _seed_tenant(repo, github_installation_id=202, account_login="tenant-b")
        run_a = _seed_run(repo, tenant_a.id, repo_name="shared/repo", run_id="run-a")
        run_b = _seed_run(repo, tenant_b.id, repo_name="shared/repo", run_id="run-b")

    client = TestClient(app)
    tenant_a_runs = client.get(
        "/platform/runs",
        headers={"X-MergeWarden-Tenant": str(tenant_a.id)},
    )
    tenant_a_detail_for_b = client.get(
        f"/platform/runs/{run_b}",
        headers={"X-MergeWarden-Tenant": str(tenant_a.id)},
    )
    tenant_a_retry_for_b = client.post(
        f"/platform/runs/{run_b}/retry",
        headers={"X-MergeWarden-Tenant": str(tenant_a.id)},
    )
    tenant_b_runs = client.get(
        "/platform/runs",
        headers={"X-MergeWarden-Tenant": str(tenant_b.id)},
    )

    assert tenant_a_runs.status_code == 200
    assert [item["run_id"] for item in tenant_a_runs.json()] == [run_a]
    assert tenant_a_detail_for_b.status_code == 404
    assert tenant_a_detail_for_b.json() == {"message": "run not found", "run_id": run_b}
    assert tenant_a_retry_for_b.status_code == 404
    assert tenant_a_retry_for_b.json() == {"message": "run not found", "run_id": run_b}
    assert tenant_b_runs.status_code == 200
    assert [item["run_id"] for item in tenant_b_runs.json()] == [run_b]


def _configure_platform(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("PLATFORM_DEFAULT_TENANT_ID", raising=False)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'platform.db'}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLATFORM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_PUBLISH_COMMENTS", "true")
    monkeypatch.setenv("GITHUB_REVIEW_DRAFT_PRS", "false")


def _seed_tenant(repo, *, github_installation_id: int, account_login: str):  # type: ignore[no-untyped-def]
    return repo.upsert_installation(
        github_installation_id=github_installation_id,
        account_login=account_login,
        account_type="Organization",
        status="active",
    )


def _seed_run(repo, installation_id: int, *, repo_name: str, run_id: str) -> str:  # type: ignore[no-untyped-def]
    owner, name = repo_name.split("/", 1)
    repository = repo.upsert_repository(
        installation_id=installation_id,
        full_name=repo_name,
        owner=owner,
        name=name,
        default_branch="main",
        enabled=True,
    )
    run = repo.create_review_run(
        installation_id=installation_id,
        repository_id=repository.id,
        repo_full_name=repo_name,
        pr_number=7,
        head_sha="same-head-sha",
        base_sha="base-sha",
        trigger_event="pull_request",
        trigger_action="opened",
        run_id=run_id,
    )
    return run.run_id
