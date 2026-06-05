"""Pure webhook helper tests that do not require FastAPI."""

from __future__ import annotations

import hashlib
import hmac

from src.config import Settings
from src.integrations.github_webhook import (
    MemoryWebhookIdempotencyStore,
    claim_webhook_work,
    decide_github_webhook,
    verify_github_webhook_signature,
)


def test_verify_github_webhook_signature_accepts_valid_signature() -> None:
    body = b'{"zen":"ok"}'

    assert verify_github_webhook_signature(
        body=body,
        signature_header=_signature(body, "secret"),
        secret="secret",
    )


def test_verify_github_webhook_signature_rejects_invalid_signature() -> None:
    body = b'{"zen":"ok"}'

    assert not verify_github_webhook_signature(
        body=body,
        signature_header=_signature(body, "wrong"),
        secret="secret",
    )


def test_verify_github_webhook_signature_rejects_missing_signature() -> None:
    assert not verify_github_webhook_signature(
        body=b"{}",
        signature_header=None,
        secret="secret",
    )


def test_verify_github_webhook_signature_rejects_missing_secret() -> None:
    body = b"{}"

    assert not verify_github_webhook_signature(
        body=body,
        signature_header=_signature(body, "secret"),
        secret="",
    )


def test_ping_event_is_accepted_without_review_trigger() -> None:
    decision = decide_github_webhook(
        event_name="ping",
        delivery_id="delivery-ping",
        payload={"zen": "ok"},
        settings=Settings(),
    )

    assert decision.status == "ok"
    assert decision.reason == "ping"
    assert decision.trigger is None


def test_unsupported_event_is_ignored() -> None:
    decision = decide_github_webhook(
        event_name="issues",
        delivery_id="delivery-issues",
        payload={"action": "opened"},
        settings=Settings(),
    )

    assert decision.status == "ignored"
    assert decision.reason == "unsupported_event:issues"


def test_pull_request_opened_decision_builds_review_trigger() -> None:
    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-opened",
        payload=_pr_payload("opened"),
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "accepted"
    assert decision.trigger is not None
    assert decision.trigger.owner_repo == "owner/repo"
    assert decision.trigger.pull_number == 7
    assert decision.trigger.head_sha == "head-sha"
    assert decision.trigger.installation_id == 123


def test_pull_request_synchronize_decision_builds_review_trigger() -> None:
    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-sync",
        payload=_pr_payload("synchronize"),
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "accepted"
    assert decision.trigger is not None
    assert decision.trigger.action == "synchronize"


def test_unsupported_pull_request_action_is_ignored() -> None:
    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-edited",
        payload=_pr_payload("edited"),
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "ignored"
    assert decision.reason == "unsupported_action:edited"


def test_converted_to_draft_action_is_ignored() -> None:
    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-draft-action",
        payload=_pr_payload("converted_to_draft"),
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "ignored"
    assert decision.reason == "unsupported_action:converted_to_draft"


def test_draft_pull_request_is_skipped_by_default() -> None:
    payload = _pr_payload("opened")
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["draft"] = True

    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-draft-pr",
        payload=payload,
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "ignored"
    assert decision.reason == "draft_pull_request"


def test_app_mode_without_installation_id_is_ignored() -> None:
    payload = _pr_payload("opened")
    payload.pop("installation")

    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-missing-installation",
        payload=payload,
        settings=Settings(github_auth_mode="app"),
    )

    assert decision.status == "ignored"
    assert decision.reason == "missing_installation_id"


def test_token_mode_accepts_pull_request_without_installation_id() -> None:
    payload = _pr_payload("opened")
    payload.pop("installation")

    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-token-mode",
        payload=payload,
        settings=Settings(github_auth_mode="token"),
    )

    assert decision.status == "accepted"
    assert decision.trigger is not None
    assert decision.trigger.installation_id is None


def test_missing_repository_or_pull_request_fields_are_ignored() -> None:
    missing_repo = _pr_payload("opened")
    missing_repo.pop("repository")
    missing_head = _pr_payload("opened")
    pull_request = missing_head["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["head"] = {}

    repo_decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-missing-repo",
        payload=missing_repo,
        settings=Settings(github_auth_mode="app"),
    )
    head_decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-missing-head",
        payload=missing_head,
        settings=Settings(github_auth_mode="app"),
    )

    assert repo_decision.status == "ignored"
    assert repo_decision.reason == "missing_required_pull_request_fields"
    assert head_decision.status == "ignored"
    assert head_decision.reason == "missing_required_pull_request_fields"


def test_duplicate_delivery_and_duplicate_review_head_are_not_reclaimed() -> None:
    store = MemoryWebhookIdempotencyStore()
    decision = decide_github_webhook(
        event_name="pull_request",
        delivery_id="delivery-1",
        payload=_pr_payload("opened"),
        settings=Settings(github_auth_mode="app"),
    )

    first = claim_webhook_work(
        decision=decision,
        delivery_id="delivery-1",
        allow_rerun=False,
        store=store,
    )
    duplicate_delivery = claim_webhook_work(
        decision=decision,
        delivery_id="delivery-1",
        allow_rerun=False,
        store=store,
    )
    duplicate_review = claim_webhook_work(
        decision=decision,
        delivery_id="delivery-2",
        allow_rerun=False,
        store=store,
    )

    assert first.status == "accepted"
    assert duplicate_delivery.status == "duplicate"
    assert duplicate_delivery.reason == "duplicate_delivery"
    assert duplicate_review.status == "duplicate"
    assert duplicate_review.reason == "duplicate_review_head"


def _pr_payload(action: str) -> dict[str, object]:
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


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
