"""Signed offline webhook coverage for GitHub App lifecycle state."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.api.app import app
from src.config import get_settings
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository

_SECRET = "offline-lifecycle-secret"
_INSTALLATION_ID = 4242


def test_installation_lifecycle_blocks_and_restores_enqueue(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        created = _post(
            client,
            "installation",
            "installation-created",
            _installation_payload("created"),
        )
        first_review = _post(
            client,
            "pull_request",
            "review-before-suspend",
            _pull_request_payload("a" * 40),
        )
        suspended = _post(
            client,
            "installation",
            "installation-suspend",
            _installation_payload("suspend"),
        )
        blocked = _post(
            client,
            "pull_request",
            "review-while-suspended",
            _pull_request_payload("c" * 40),
        )
        unsuspended = _post(
            client,
            "installation",
            "installation-unsuspend",
            _installation_payload("unsuspend"),
        )
        restored = _post(
            client,
            "pull_request",
            "review-after-unsuspend",
            _pull_request_payload("d" * 40),
        )
        deleted = _post(
            client,
            "installation",
            "installation-deleted",
            _installation_payload("deleted"),
        )
        blocked_after_delete = _post(
            client,
            "pull_request",
            "review-after-delete",
            _pull_request_payload("e" * 40),
        )

    assert created.json()["reason"] == "installation_created"
    assert first_review.json()["status"] == "queued"
    assert suspended.json()["reason"] == "installation_suspend"
    assert blocked.json() == {
        "status": "ignored",
        "delivery_id": "review-while-suspended",
        "reason": "installation_suspended",
        "owner_repo": "owner/repo",
        "pull_number": 7,
        "head_sha": "c" * 40,
    }
    assert unsuspended.json()["reason"] == "installation_unsuspend"
    assert restored.json()["status"] == "queued"
    assert deleted.json()["reason"] == "installation_deleted"
    assert blocked_after_delete.json()["reason"] == "installation_deleted"

    repository = _repository()
    installation = repository.get_installation_by_github_id(_INSTALLATION_ID)
    runs = repository.list_runs()
    repository.conn.close()
    assert installation is not None
    assert installation.status == "deleted"
    assert [run.head_sha for run in reversed(runs)] == ["a" * 40, "d" * 40]


def test_repository_lifecycle_preserves_disabled_state_until_added(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    with TestClient(app) as client:
        _post(
            client,
            "installation",
            "repo-flow-installation",
            _installation_payload("created"),
        )
        added = _post(
            client,
            "installation_repositories",
            "repositories-added",
            _repositories_payload("added"),
        )
        queued_before_remove = _post(
            client,
            "pull_request",
            "review-before-remove",
            _pull_request_payload("1" * 40),
        )
        removed = _post(
            client,
            "installation_repositories",
            "repositories-removed",
            _repositories_payload("removed"),
        )
        blocked = _post(
            client,
            "pull_request",
            "review-after-remove",
            _pull_request_payload("2" * 40),
        )

        repository = _repository()
        installation = repository.get_installation_by_github_id(_INSTALLATION_ID)
        assert installation is not None
        disabled = repository.get_repository(
            installation_id=installation.id,
            full_name="owner/repo",
        )
        repository.conn.close()

        readded = _post(
            client,
            "installation_repositories",
            "repositories-readded",
            _repositories_payload("added"),
        )
        restored = _post(
            client,
            "pull_request",
            "review-after-readd",
            _pull_request_payload("3" * 40),
        )

    assert added.json()["reason"] == "repositories_added"
    assert queued_before_remove.json()["status"] == "queued"
    assert removed.json()["reason"] == "repositories_removed"
    assert blocked.json()["reason"] == "repository_disabled"
    assert disabled is not None
    assert disabled.enabled is False
    assert readded.json()["reason"] == "repositories_added"
    assert restored.json()["status"] == "queued"

    repository = _repository()
    installation = repository.get_installation_by_github_id(_INSTALLATION_ID)
    assert installation is not None
    enabled = repository.get_repository(
        installation_id=installation.id,
        full_name="owner/repo",
    )
    runs = repository.list_runs()
    repository.conn.close()
    assert enabled is not None
    assert enabled.id == disabled.id
    assert enabled.enabled is True
    assert {run.head_sha for run in runs} == {"1" * 40, "3" * 40}


def _configure(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv(
        "PLATFORM_DATABASE_URL",
        f"sqlite:///{tmp_path / 'platform.db'}",
    )
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLATFORM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("GITHUB_REVIEW_DRAFT_PRS", "false")


def _repository() -> PlatformRepository:
    connection = connect(get_settings().platform_database_url)
    init_db(connection)
    return PlatformRepository(connection)


def _post(
    client: TestClient,
    event: str,
    delivery_id: str,
    payload: dict[str, Any],
):  # type: ignore[no-untyped-def]
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )


def _installation_payload(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {
            "id": _INSTALLATION_ID,
            "account": {"login": "owner", "type": "Organization"},
        },
    }


def _repositories_payload(action: str) -> dict[str, Any]:
    key = "repositories_added" if action == "added" else "repositories_removed"
    return {
        **_installation_payload(action),
        key: [
            {
                "id": 99,
                "full_name": "owner/repo",
                "name": "repo",
                "owner": {"login": "owner"},
                "default_branch": "main",
            }
        ],
    }


def _pull_request_payload(head_sha: str) -> dict[str, Any]:
    return {
        "action": "opened",
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
            "head": {"sha": head_sha},
            "base": {"sha": "b" * 40},
        },
        "installation": {
            "id": _INSTALLATION_ID,
            "account": {"login": "owner", "type": "Organization"},
        },
    }
