"""Tests for GitHub App webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


def test_github_webhook_accepts_valid_ping_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")

    response = _post_webhook(api_app.app, "ping", "delivery-ping", {"zen": "ok"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "reason": "ping",
        "delivery_id": "delivery-ping",
    }


def test_github_webhook_rejects_invalid_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    body = _body({"zen": "ok"})

    response = TestClient(api_app.app).post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "delivery-bad",
            "X-Hub-Signature-256": _signature(body, "wrong-secret"),
        },
    )

    assert response.status_code == 401
    assert response.json()["message"] == "invalid signature"


def test_github_webhook_rejects_missing_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")

    response = TestClient(api_app.app).post(
        "/github/webhook",
        content=_body({"zen": "ok"}),
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "delivery-missing-signature",
        },
    )

    assert response.status_code == 401
    assert response.json()["message"] == "invalid signature"


def test_github_webhook_unsupported_event_is_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")

    response = _post_webhook(api_app.app, "issues", "delivery-issues", {"action": "opened"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "unsupported_event:issues"


def test_github_webhook_pull_request_opened_triggers_review(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    seen: list[dict[str, object]] = []

    async def _record(trigger):  # type: ignore[no-untyped-def]
        seen.append(trigger.model_dump())

    monkeypatch.setattr(api_app, "process_github_pull_request_review", _record)

    response = _post_webhook(api_app.app, "pull_request", "delivery-opened", _pr_payload("opened"))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert seen[0]["owner_repo"] == "owner/repo"
    assert seen[0]["pull_number"] == 7
    assert seen[0]["head_sha"] == "head-sha"
    assert seen[0]["installation_id"] == 123


def test_github_webhook_pull_request_synchronize_triggers_review(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    seen: list[str] = []

    async def _record(trigger):  # type: ignore[no-untyped-def]
        seen.append(trigger.action)

    monkeypatch.setattr(api_app, "process_github_pull_request_review", _record)

    response = _post_webhook(
        api_app.app,
        "pull_request",
        "delivery-sync",
        _pr_payload("synchronize"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert seen == ["synchronize"]


def test_github_webhook_unsupported_pull_request_action_is_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    seen: list[str] = []

    async def _record(trigger):  # type: ignore[no-untyped-def]
        seen.append(trigger.action)

    monkeypatch.setattr(api_app, "process_github_pull_request_review", _record)

    response = _post_webhook(api_app.app, "pull_request", "delivery-edited", _pr_payload("edited"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "unsupported_action:edited"
    assert seen == []


def test_github_webhook_app_mode_skips_missing_installation_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    payload = _pr_payload("opened")
    payload.pop("installation")

    response = _post_webhook(api_app.app, "pull_request", "delivery-no-install", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "missing_installation_id"


def test_github_webhook_draft_pull_request_is_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    payload = _pr_payload("opened")
    payload["pull_request"]["draft"] = True

    response = _post_webhook(api_app.app, "pull_request", "delivery-draft", payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "draft_pull_request"


def test_github_webhook_duplicate_delivery_does_not_trigger_twice(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app
    from src.integrations.github_webhook import webhook_idempotency_store

    webhook_idempotency_store.clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    seen: list[str] = []

    async def _record(trigger):  # type: ignore[no-untyped-def]
        seen.append(trigger.delivery_id)

    monkeypatch.setattr(api_app, "process_github_pull_request_review", _record)
    payload = _pr_payload("opened")

    first = _post_webhook(api_app.app, "pull_request", "delivery-dup", payload)
    second = _post_webhook(api_app.app, "pull_request", "delivery-dup", payload)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert second.json()["reason"] == "duplicate_delivery"
    assert seen == ["delivery-dup"]


def _pr_payload(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 7,
            "draft": False,
            "head": {"sha": "head-sha"},
            "base": {"sha": "base-sha"},
        },
        "installation": {"id": 123},
    }


def _post_webhook(app, event: str, delivery: str, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    body = _body(payload)
    return TestClient(app).post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _signature(body, "secret"),
        },
    )


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
