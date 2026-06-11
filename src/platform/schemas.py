"""Pydantic API schemas for the platform MVP."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.platform.models import (
    InstallationRecord,
    RepositoryRecord,
    ReviewRunRecord,
    UsageRecord,
    WebhookDeliveryRecord,
)


class WebhookIngestionResponse(BaseModel):
    status: str
    delivery_id: str
    reason: str = ""
    run_id: str = ""
    owner_repo: str = ""
    pull_number: int | None = None
    head_sha: str = ""


class PlatformHealthResponse(BaseModel):
    status: str = "ok"
    database_url: str
    database_connected: bool
    worker: dict[str, object] = Field(default_factory=dict)
    artifact_root: str


class InstallationResponse(InstallationRecord):
    pass


class RepositoryResponse(RepositoryRecord):
    pass


class UsageRecordResponse(UsageRecord):
    pass


class ReviewRunResponse(ReviewRunRecord):
    artifact_paths: dict[str, str] = Field(default_factory=dict)


class ReviewRunDetailResponse(ReviewRunResponse):
    usage_records: list[UsageRecordResponse] = Field(default_factory=list)


class WebhookDeliveryResponse(WebhookDeliveryRecord):
    pass


class RetryRunResponse(BaseModel):
    status: str
    run_id: str
    previous_run_id: str
