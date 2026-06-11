"""Tenant configuration resolution for platform runs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import Settings
from src.platform.models import TenantConfigRecord
from src.platform.repositories import PlatformRepository


class EffectiveTenantConfig(BaseModel):
    review_enabled: bool = True
    review_draft_prs: bool = False
    publish_comments: bool = True
    model_name: str
    token_budget: int = Field(ge=1)
    prompt_input_token_budget: int = Field(ge=1)


def resolve_effective_config(
    repo: PlatformRepository,
    *,
    settings: Settings,
    installation_id: int,
    repository_id: int | None,
) -> EffectiveTenantConfig:
    """Resolve repo > installation > global settings for one run."""
    installation_config = repo.get_tenant_config(
        installation_id=installation_id,
        repository_id=None,
    )
    repository_config = (
        repo.get_tenant_config(
            installation_id=installation_id,
            repository_id=repository_id,
        )
        if repository_id is not None
        else None
    )
    return EffectiveTenantConfig(
        review_enabled=_bool_value(
            "review_enabled",
            repository_config,
            installation_config,
            settings.platform_review_enabled,
        ),
        review_draft_prs=_bool_value(
            "review_draft_prs",
            repository_config,
            installation_config,
            settings.github_review_draft_prs,
        ),
        publish_comments=_bool_value(
            "publish_comments",
            repository_config,
            installation_config,
            settings.platform_publish_comments,
        ),
        model_name=_optional_value(
            "model_name",
            repository_config,
            installation_config,
            settings.model_name,
        ),
        token_budget=_optional_value(
            "token_budget",
            repository_config,
            installation_config,
            settings.token_budget,
        ),
        prompt_input_token_budget=_optional_value(
            "prompt_input_token_budget",
            repository_config,
            installation_config,
            settings.prompt_input_token_budget,
        ),
    )


def _bool_value(
    field_name: str,
    repository_config: TenantConfigRecord | None,
    installation_config: TenantConfigRecord | None,
    default: bool,
) -> bool:
    for record in (repository_config, installation_config):
        if record is not None:
            return bool(getattr(record, field_name))
    return bool(default)


def _optional_value(
    field_name: str,
    repository_config: TenantConfigRecord | None,
    installation_config: TenantConfigRecord | None,
    default,
):
    for record in (repository_config, installation_config):
        if record is None:
            continue
        value = getattr(record, field_name)
        if value not in {None, ""}:
            return value
    return default
