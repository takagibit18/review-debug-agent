"""Platform MVP tests for DB-backed webhook intake, worker, and admin APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest


def test_webhook_delivery_idempotency_creates_one_queued_run(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from src.api.app import app

    _configure_platform(monkeypatch, tmp_path)

    first = _post_webhook(app, "pull_request", "delivery-1", _pr_payload("opened"))
    second = _post_webhook(app, "pull_request", "delivery-1", _pr_payload("opened"))
    client = TestClient(app)
    runs = client.get("/platform/runs", headers=_tenant_headers(client)).json()

    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    assert first.json()["run_id"]
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["reason"] == "duplicate_delivery"
    assert len(runs) == 1
    assert runs[0]["run_id"] == first.json()["run_id"]
    assert runs[0]["status"] == "queued"


def test_webhook_repo_pr_head_idempotency_reuses_existing_run(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from src.api.app import app

    _configure_platform(monkeypatch, tmp_path)

    first = _post_webhook(app, "pull_request", "delivery-1", _pr_payload("opened"))
    second = _post_webhook(app, "pull_request", "delivery-2", _pr_payload("opened"))

    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate"
    assert second.json()["reason"] == "duplicate_review_head"
    assert second.json()["run_id"] == first.json()["run_id"]
    client = TestClient(app)
    assert len(client.get("/platform/runs", headers=_tenant_headers(client)).json()) == 1


def test_ignored_event_and_draft_pr_are_recorded(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from src.api.app import app

    _configure_platform(monkeypatch, tmp_path)
    draft_payload = _pr_payload("opened")
    draft_payload["pull_request"]["draft"] = True

    ignored_event = _post_webhook(app, "issues", "delivery-issues", {"action": "opened"})
    draft = _post_webhook(app, "pull_request", "delivery-draft", draft_payload)
    client = TestClient(app)
    deliveries = client.get("/platform/deliveries", headers=_tenant_headers(client)).json()

    assert ignored_event.json()["status"] == "ignored"
    assert ignored_event.json()["reason"] == "unsupported_event:issues"
    assert draft.json()["status"] == "ignored"
    assert draft.json()["reason"] == "draft_pull_request"
    assert {(item["delivery_id"], item["status"], item["reason"]) for item in deliveries} == {
        ("delivery-draft", "ignored", "draft_pull_request"),
    }


def test_tenant_config_priority_prefers_repo_over_installation_and_settings(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.config import get_settings
    from src.platform.config import resolve_effective_config
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        installation = repo.upsert_installation(
            github_installation_id=123,
            account_login="owner",
            account_type="User",
            status="active",
        )
        repository = repo.upsert_repository(
            installation_id=installation.id,
            full_name="owner/repo",
            owner="owner",
            name="repo",
            default_branch="main",
            enabled=True,
        )
        repo.upsert_tenant_config(
            installation_id=installation.id,
            repository_id=None,
            review_enabled=False,
            review_draft_prs=False,
            publish_comments=True,
            model_name="install-model",
            token_budget=1000,
            prompt_input_token_budget=900,
        )
        repo.upsert_tenant_config(
            installation_id=installation.id,
            repository_id=repository.id,
            review_enabled=True,
            review_draft_prs=True,
            publish_comments=False,
            model_name="repo-model",
            token_budget=2000,
            prompt_input_token_budget=1800,
        )

        effective = resolve_effective_config(
            repo,
            settings=settings,
            installation_id=installation.id,
            repository_id=repository.id,
        )

    assert effective.review_enabled is True
    assert effective.review_draft_prs is True
    assert effective.publish_comments is False
    assert effective.model_name == "repo-model"
    assert effective.token_budget == 2000
    assert effective.prompt_input_token_budget == 1800


def test_worker_success_updates_status_usage_and_artifact_paths(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository
    from src.platform.worker import PlatformWorker, ReviewPipelineResult

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_queued_run(repo)

        async def fake_pipeline(run, config):  # type: ignore[no-untyped-def]
            return ReviewPipelineResult(
                review_response={"run_id": run.run_id, "report": {"summary": "ok", "issues": []}},
                publish_result={"status": "published", "run_id": run.run_id},
                run_summary={
                    "run_id": run.run_id,
                    "model_names": [config.model_name],
                    "total_tokens": 42,
                },
                diff_text="diff --git a/a.py b/a.py\n",
                changed_lines={"a.py": [1]},
                prompt_tokens=20,
                completion_tokens=22,
                total_tokens=42,
                duration_ms=123,
                model_name=config.model_name,
            )

        processed = PlatformWorker(repo, settings=settings, pipeline=fake_pipeline).run_once()
        run = repo.get_run(run_id)
        usage = repo.list_usage_records(run_id=run_id)

    assert processed is True
    assert run is not None
    assert run.status == "succeeded"
    assert run.review_response_path.endswith("review_response.json")
    assert run.run_summary_path.endswith("run_summary.json")
    assert run.publish_result_path.endswith("publish_result.json")
    assert Path(settings.platform_artifact_root, run_id, "review_response.json").exists()
    assert Path(settings.platform_artifact_root, run_id, "changed_lines.json").exists()
    assert usage[0].total_tokens == 42
    assert usage[0].duration_ms == 123


def test_worker_failure_marks_run_failed_without_crashing(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository
    from src.platform.worker import PlatformWorker

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_queued_run(repo)

        async def failing_pipeline(run, config):  # type: ignore[no-untyped-def]
            raise RuntimeError("model provider unavailable")

        processed = PlatformWorker(repo, settings=settings, pipeline=failing_pipeline).run_once()
        run = repo.get_run(run_id)

    assert processed is True
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "RuntimeError"
    assert "model provider unavailable" in run.error_message


def test_platform_runs_query_detail_and_retry(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from src.api.app import app
    from src.config import get_settings
    from src.platform.db import connect, init_db
    from src.platform.repositories import PlatformRepository

    _configure_platform(monkeypatch, tmp_path)
    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repo = PlatformRepository(conn)
        run_id = _seed_queued_run(repo)
        repo.mark_run_failed(run_id, error_type="RuntimeError", error_message="boom")

    client = TestClient(app)
    headers = _tenant_headers(client)
    failed_runs = client.get(
        "/platform/runs",
        params={"status": "failed"},
        headers=headers,
    ).json()
    detail = client.get(f"/platform/runs/{run_id}", headers=headers).json()
    retry = client.post(f"/platform/runs/{run_id}/retry", headers=headers)

    assert failed_runs[0]["run_id"] == run_id
    assert detail["run_id"] == run_id
    assert detail["status"] == "failed"
    assert detail["usage_records"] == []
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"
    assert retry.json()["run_id"] != run_id


def _configure_platform(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{tmp_path / 'platform.db'}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLATFORM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_PUBLISH_COMMENTS", "true")
    monkeypatch.setenv("GITHUB_REVIEW_DRAFT_PRS", "false")


def _seed_queued_run(repo) -> str:  # type: ignore[no-untyped-def]
    installation = repo.upsert_installation(
        github_installation_id=123,
        account_login="owner",
        account_type="User",
        status="active",
    )
    repository = repo.upsert_repository(
        installation_id=installation.id,
        full_name="owner/repo",
        owner="owner",
        name="repo",
        default_branch="main",
        enabled=True,
    )
    run = repo.create_review_run(
        installation_id=installation.id,
        repository_id=repository.id,
        repo_full_name="owner/repo",
        pr_number=7,
        head_sha="head-sha",
        base_sha="base-sha",
        trigger_event="pull_request",
        trigger_action="opened",
    )
    return run.run_id


def _pr_payload(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "number": 7,
        "repository": {
            "full_name": "owner/repo",
            "name": "repo",
            "owner": {"login": "owner"},
            "default_branch": "main",
        },
        "pull_request": {
            "number": 7,
            "draft": False,
            "head": {"sha": "head-sha"},
            "base": {"sha": "base-sha"},
        },
        "installation": {"id": 123, "account": {"login": "owner", "type": "User"}},
    }


def _post_webhook(app, event: str, delivery: str, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return TestClient(app).post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _signature(body, "secret"),
        },
    )


def _tenant_headers(client) -> dict[str, str]:  # type: ignore[no-untyped-def]
    tenants = client.get("/platform/installations").json()
    assert tenants
    return {"X-MergeWarden-Tenant": str(tenants[0]["id"])}


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
