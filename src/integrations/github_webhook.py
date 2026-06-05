"""GitHub webhook parsing, signature verification, and idempotency helpers."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from src.config import Settings
from src.integrations.github_pr_review import GitHubPullRequestReviewTrigger

logger = logging.getLogger(__name__)

SUPPORTED_PULL_REQUEST_ACTIONS = {
    "opened",
    "reopened",
    "synchronize",
    "ready_for_review",
}


class WebhookDecision(BaseModel):
    """Normalized webhook handling decision."""

    status: str
    reason: str = ""
    trigger: GitHubPullRequestReviewTrigger | None = None
    review_key: str = ""


@dataclass
class MemoryWebhookIdempotencyStore:
    """Process-local webhook idempotency store.

    This is intentionally lightweight for the first GitHub App loop. It prevents
    duplicate work while the process is alive; production deployments with
    multiple replicas should replace it with shared storage.
    """

    delivery_ids: set[str] = field(default_factory=set)
    review_keys: set[str] = field(default_factory=set)

    def claim_delivery(self, delivery_id: str) -> bool:
        if not delivery_id:
            return True
        if delivery_id in self.delivery_ids:
            return False
        self.delivery_ids.add(delivery_id)
        return True

    def claim_review(self, review_key: str) -> bool:
        if not review_key:
            return True
        if review_key in self.review_keys:
            return False
        self.review_keys.add(review_key)
        return True

    def clear(self) -> None:
        self.delivery_ids.clear()
        self.review_keys.clear()


webhook_idempotency_store = MemoryWebhookIdempotencyStore()


def verify_github_webhook_signature(
    *,
    body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify GitHub X-Hub-Signature-256 against the raw request body."""
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature_header)


def decide_github_webhook(
    *,
    event_name: str,
    delivery_id: str,
    payload: dict[str, Any],
    settings: Settings,
) -> WebhookDecision:
    """Return how the webhook should be handled."""
    if event_name == "ping":
        return WebhookDecision(status="ok", reason="ping")
    if event_name in {"installation", "installation_repositories"}:
        logger.info(
            "github installation event received",
            extra={
                "delivery_id": delivery_id,
                "event_name": event_name,
                "action": str(payload.get("action", "") or ""),
                "installation_id": _installation_id(payload),
            },
        )
        return WebhookDecision(status="ignored", reason=event_name)
    if event_name != "pull_request":
        return WebhookDecision(status="ignored", reason=f"unsupported_event:{event_name}")

    action = str(payload.get("action", "") or "")
    if action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return WebhookDecision(status="ignored", reason=f"unsupported_action:{action}")

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return WebhookDecision(status="ignored", reason="missing_pull_request")
    if bool(pull_request.get("draft", False)) and not settings.github_review_draft_prs:
        return WebhookDecision(status="ignored", reason="draft_pull_request")

    owner_repo = _owner_repo(payload)
    pull_number = _pull_number(payload, pull_request)
    head_sha = _nested_str(pull_request, "head", "sha")
    base_sha = _nested_str(pull_request, "base", "sha")
    installation_id = _installation_id(payload)
    if not owner_repo or not pull_number or not head_sha or not base_sha:
        return WebhookDecision(status="ignored", reason="missing_required_pull_request_fields")
    if settings.github_auth_mode == "app" and installation_id is None:
        return WebhookDecision(status="ignored", reason="missing_installation_id")

    trigger = GitHubPullRequestReviewTrigger(
        owner_repo=owner_repo,
        pull_number=pull_number,
        head_sha=head_sha,
        base_sha=base_sha,
        installation_id=installation_id,
        trigger_event=event_name,
        delivery_id=delivery_id,
        action=action,
    )
    return WebhookDecision(
        status="accepted",
        trigger=trigger,
        review_key=f"{owner_repo}:{pull_number}:{head_sha}",
    )


def claim_webhook_work(
    *,
    decision: WebhookDecision,
    delivery_id: str,
    allow_rerun: bool,
    store: MemoryWebhookIdempotencyStore = webhook_idempotency_store,
) -> WebhookDecision:
    """Apply lightweight idempotency for accepted webhook work."""
    if decision.status != "accepted" or allow_rerun:
        return decision
    if not store.claim_delivery(delivery_id):
        return WebhookDecision(status="duplicate", reason="duplicate_delivery")
    if not store.claim_review(decision.review_key):
        return WebhookDecision(status="duplicate", reason="duplicate_review_head")
    return decision


def _owner_repo(payload: dict[str, Any]) -> str:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return ""
    return str(repository.get("full_name", "") or "")


def _pull_number(payload: dict[str, Any], pull_request: dict[str, Any]) -> int:
    raw = payload.get("number", pull_request.get("number"))
    if raw is None:
        return 0
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def _installation_id(payload: dict[str, Any]) -> int | None:
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return None
    raw_id = installation.get("id")
    if raw_id is None:
        return None
    try:
        return int(str(raw_id))
    except (TypeError, ValueError):
        return None


def _nested_str(payload: dict[str, Any], first: str, second: str) -> str:
    first_value = payload.get(first)
    if not isinstance(first_value, dict):
        return ""
    return str(first_value.get(second, "") or "")
