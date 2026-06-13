"""Database-backed platform records."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReviewRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "skipped"]
WebhookDeliveryStatus = Literal["received", "duplicate", "queued", "ignored", "failed"]


class InstallationRecord(BaseModel):
    id: int
    github_installation_id: int
    account_login: str = ""
    account_type: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class RepositoryRecord(BaseModel):
    id: int
    installation_id: int
    full_name: str
    owner: str = ""
    name: str = ""
    default_branch: str = ""
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class ReviewRunRecord(BaseModel):
    id: int
    run_id: str
    installation_id: int
    repository_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    status: ReviewRunStatus
    trigger_event: str = ""
    trigger_action: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error_type: str = ""
    error_message: str = ""
    review_response_path: str = ""
    run_summary_path: str = ""
    publish_result_path: str = ""
    total_tokens: int | None = None
    publish_status: str = ""
    created_at: str = ""
    updated_at: str = ""


class WebhookDeliveryRecord(BaseModel):
    id: int
    delivery_id: str
    installation_id: int | None = None
    event: str = ""
    action: str = ""
    repo_full_name: str = ""
    pr_number: int | None = None
    head_sha: str = ""
    status: WebhookDeliveryStatus
    reason: str = ""
    run_id: str = ""
    received_at: str = ""


class TenantConfigRecord(BaseModel):
    id: int
    installation_id: int
    repository_id: int | None = None
    review_enabled: bool = True
    review_draft_prs: bool = False
    publish_comments: bool = True
    model_name: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    prompt_input_token_budget: int | None = Field(default=None, ge=1)
    created_at: str = ""
    updated_at: str = ""


class UsageRecord(BaseModel):
    id: int
    run_id: str
    installation_id: int
    repository_id: int
    model_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    created_at: str = ""
