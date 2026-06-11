"""Platform service layer for webhook intake and run queries."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from src.config import Settings
from src.integrations.github_webhook import SUPPORTED_PULL_REQUEST_ACTIONS
from src.platform.config import resolve_effective_config
from src.platform.models import RepositoryRecord, ReviewRunRecord
from src.platform.repositories import PlatformRepository
from src.platform.schemas import WebhookIngestionResponse


@dataclass(frozen=True)
class PullRequestWebhookFields:
    action: str = ""
    repo_full_name: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    default_branch: str = ""
    pr_number: int | None = None
    head_sha: str = ""
    base_sha: str = ""
    draft: bool = False
    github_installation_id: int | None = None
    account_login: str = ""
    account_type: str = ""


class WebhookIngestionService:
    """Persist GitHub webhook deliveries and enqueue durable review runs."""

    def __init__(self, repo: PlatformRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    def ingest(
        self,
        *,
        event_name: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> WebhookIngestionResponse:
        fields = parse_pull_request_webhook_fields(payload)
        delivery, duplicate_delivery = self.repo.insert_webhook_delivery(
            delivery_id=delivery_id or "",
            event=event_name,
            action=fields.action,
            repo_full_name=fields.repo_full_name,
            pr_number=fields.pr_number,
            head_sha=fields.head_sha,
        )
        if duplicate_delivery:
            return WebhookIngestionResponse(
                status="duplicate",
                reason="duplicate_delivery",
                delivery_id=delivery_id,
                run_id=delivery.run_id,
                owner_repo=delivery.repo_full_name,
                pull_number=delivery.pr_number,
                head_sha=delivery.head_sha,
            )

        if event_name == "ping":
            self.repo.update_delivery_status(delivery_id, status="ignored", reason="ping")
            return WebhookIngestionResponse(status="ok", reason="ping", delivery_id=delivery_id)

        if event_name in {"installation", "installation_repositories"}:
            self._upsert_installation_if_present(fields)
            self.repo.update_delivery_status(
                delivery_id,
                status="ignored",
                reason=event_name,
            )
            return WebhookIngestionResponse(
                status="ignored",
                reason=event_name,
                delivery_id=delivery_id,
            )

        if event_name != "pull_request":
            return self._ignore(
                delivery_id,
                reason=f"unsupported_event:{event_name}",
                fields=fields,
            )

        if fields.action not in SUPPORTED_PULL_REQUEST_ACTIONS:
            return self._ignore(
                delivery_id,
                reason=f"unsupported_action:{fields.action}",
                fields=fields,
            )

        if not fields.repo_full_name or not fields.pr_number or not fields.head_sha or not fields.base_sha:
            return self._ignore(
                delivery_id,
                reason="missing_required_pull_request_fields",
                fields=fields,
            )

        if self.settings.github_auth_mode == "app" and fields.github_installation_id is None:
            return self._ignore(
                delivery_id,
                reason="missing_installation_id",
                fields=fields,
            )

        installation = self._upsert_installation_if_present(fields)
        repository = self._upsert_repository_if_present(fields, installation.id)
        if not repository.enabled:
            return self._ignore(delivery_id, reason="repository_disabled", fields=fields)

        effective = resolve_effective_config(
            self.repo,
            settings=self.settings,
            installation_id=installation.id,
            repository_id=repository.id,
        )
        if not effective.review_enabled:
            return self._ignore(delivery_id, reason="review_disabled", fields=fields)
        if fields.draft and not effective.review_draft_prs:
            return self._ignore(delivery_id, reason="draft_pull_request", fields=fields)

        existing = self.repo.find_active_run(
            repo_full_name=fields.repo_full_name,
            pr_number=fields.pr_number,
            head_sha=fields.head_sha,
        )
        if existing is not None and not self.settings.github_webhook_allow_rerun:
            self.repo.update_delivery_status(
                delivery_id,
                status="duplicate",
                reason="duplicate_review_head",
                run_id=existing.run_id,
            )
            return _duplicate_review_response(delivery_id, fields, existing)

        try:
            run = self.repo.create_review_run(
                installation_id=installation.id,
                repository_id=repository.id,
                repo_full_name=fields.repo_full_name,
                pr_number=fields.pr_number,
                head_sha=fields.head_sha,
                base_sha=fields.base_sha,
                trigger_event=event_name,
                trigger_action=fields.action,
            )
        except sqlite3.IntegrityError:
            existing = self.repo.find_active_run(
                repo_full_name=fields.repo_full_name,
                pr_number=fields.pr_number,
                head_sha=fields.head_sha,
            )
            if existing is None:
                raise
            self.repo.update_delivery_status(
                delivery_id,
                status="duplicate",
                reason="duplicate_review_head",
                run_id=existing.run_id,
            )
            return _duplicate_review_response(delivery_id, fields, existing)

        self.repo.update_delivery_status(
            delivery_id,
            status="queued",
            reason="",
            run_id=run.run_id,
        )
        return WebhookIngestionResponse(
            status="queued",
            delivery_id=delivery_id,
            run_id=run.run_id,
            owner_repo=fields.repo_full_name,
            pull_number=fields.pr_number,
            head_sha=fields.head_sha,
        )

    def _upsert_installation_if_present(self, fields: PullRequestWebhookFields):
        github_installation_id = fields.github_installation_id or 0
        account_login = fields.account_login or fields.repo_owner or "unknown"
        account_type = fields.account_type or "unknown"
        return self.repo.upsert_installation(
            github_installation_id=github_installation_id,
            account_login=account_login,
            account_type=account_type,
            status="active",
        )

    def _upsert_repository_if_present(
        self,
        fields: PullRequestWebhookFields,
        installation_id: int,
    ) -> RepositoryRecord:
        return self.repo.upsert_repository(
            installation_id=installation_id,
            full_name=fields.repo_full_name,
            owner=fields.repo_owner,
            name=fields.repo_name,
            default_branch=fields.default_branch,
            enabled=True,
        )

    def _ignore(
        self,
        delivery_id: str,
        *,
        reason: str,
        fields: PullRequestWebhookFields,
    ) -> WebhookIngestionResponse:
        self.repo.update_delivery_status(delivery_id, status="ignored", reason=reason)
        return WebhookIngestionResponse(
            status="ignored",
            reason=reason,
            delivery_id=delivery_id,
            owner_repo=fields.repo_full_name,
            pull_number=fields.pr_number,
            head_sha=fields.head_sha,
        )


def parse_pull_request_webhook_fields(payload: dict[str, Any]) -> PullRequestWebhookFields:
    """Extract normalized PR/repo/installation fields from a GitHub payload."""
    action = str(payload.get("action", "") or "")
    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    pull_request = payload.get("pull_request")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    installation = payload.get("installation")
    installation = installation if isinstance(installation, dict) else {}

    full_name = str(repository.get("full_name", "") or "")
    owner_payload = repository.get("owner")
    owner_payload = owner_payload if isinstance(owner_payload, dict) else {}
    owner = str(owner_payload.get("login", "") or "")
    name = str(repository.get("name", "") or "")
    if full_name and (not owner or not name) and "/" in full_name:
        owner, name = full_name.split("/", 1)

    account = installation.get("account")
    account = account if isinstance(account, dict) else {}
    installation_id = _int_or_none(installation.get("id"))
    account_login = str(account.get("login", "") or owner)
    account_type = str(account.get("type", "") or "")

    pr_number = _int_or_none(payload.get("number", pull_request.get("number")))
    head = pull_request.get("head")
    head = head if isinstance(head, dict) else {}
    base = pull_request.get("base")
    base = base if isinstance(base, dict) else {}

    return PullRequestWebhookFields(
        action=action,
        repo_full_name=full_name,
        repo_owner=owner,
        repo_name=name,
        default_branch=str(repository.get("default_branch", "") or ""),
        pr_number=pr_number,
        head_sha=str(head.get("sha", "") or ""),
        base_sha=str(base.get("sha", "") or ""),
        draft=bool(pull_request.get("draft", False)),
        github_installation_id=installation_id,
        account_login=account_login,
        account_type=account_type,
    )


def _duplicate_review_response(
    delivery_id: str,
    fields: PullRequestWebhookFields,
    run: ReviewRunRecord,
) -> WebhookIngestionResponse:
    return WebhookIngestionResponse(
        status="duplicate",
        reason="duplicate_review_head",
        delivery_id=delivery_id,
        run_id=run.run_id,
        owner_repo=fields.repo_full_name,
        pull_number=fields.pr_number,
        head_sha=fields.head_sha,
    )


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
