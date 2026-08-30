"""Credential-free integration coverage for webhook intake and the DB worker."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import app
from src.config import get_settings
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository
from src.platform.worker import PlatformWorker, ReviewPipelineResult


def test_signed_webhook_reaches_worker_checkpoints_artifacts_and_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret = "offline-platform-secret"
    database_path = tmp_path / "platform.db"
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("PLATFORM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_PUBLISH_COMMENTS", "true")

    payload = {
        "action": "opened",
        "number": 17,
        "repository": {
            "full_name": "offline/runtime-fixture",
            "name": "runtime-fixture",
            "owner": {"login": "offline"},
            "default_branch": "main",
        },
        "pull_request": {
            "number": 17,
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
        },
        "installation": {
            "id": 9017,
            "account": {"login": "offline", "type": "Organization"},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        webhook = client.post(
            "/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "offline-delivery-17",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    assert webhook.status_code == 200
    assert webhook.json()["status"] == "queued"
    run_id = webhook.json()["run_id"]

    async def fake_pipeline(run, config):  # type: ignore[no-untyped-def]
        assert run.run_id == run_id
        assert run.head_sha == "a" * 40
        assert config.review_enabled is True
        return ReviewPipelineResult(
            review_response={
                "run_id": run_id,
                "report": {"summary": "offline integration", "issues": []},
            },
            publish_result={"status": "published", "head_sha": run.head_sha},
            run_summary={"run_id": run_id, "total_tokens": 9},
            diff_text="diff --git a/app.py b/app.py\n+offline = True\n",
            changed_lines={"app.py": [1]},
            prompt_tokens=4,
            completion_tokens=5,
            total_tokens=9,
            model_name="offline-fake-model",
        )

    settings = get_settings()
    with connect(settings.platform_database_url) as conn:
        init_db(conn)
        repository = PlatformRepository(conn)
        worker = PlatformWorker(
            repository,
            settings=settings,
            pipeline=fake_pipeline,
            worker_id="offline-worker",
        )

        assert worker.run_once() is True
        run = repository.get_run(run_id)
        delivery = repository.list_deliveries()[0]
        checkpoints = repository.list_checkpoints(run_id)
        usage = repository.list_usage_records(run_id=run_id)

    assert run is not None
    assert run.status == "succeeded"
    assert run.attempt == 1
    assert delivery.status == "queued"
    assert delivery.run_id == run_id
    assert [(item.step_id, item.status) for item in checkpoints] == [
        ("review_pipeline", "completed"),
        ("persist_artifacts", "completed"),
    ]
    assert usage[0].total_tokens == 9
    assert (artifact_root / run_id / "pipeline_result.json").exists()
    assert (artifact_root / run_id / "review_response.json").exists()
    assert (artifact_root / run_id / "pr.diff").exists()
    assert (artifact_root / run_id / "changed_lines.json").exists()
