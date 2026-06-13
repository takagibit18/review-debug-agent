"""Tenant context helpers for platform management requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.platform.models import InstallationRecord

TENANT_HEADER = "X-MergeWarden-Tenant"
TenantSource = Literal["header", "default"]


class TenantResolutionError(ValueError):
    """Raised when a request carries an invalid tenant identifier."""


@dataclass(frozen=True)
class TenantContext:
    id: int
    name: str
    github_installation_id: int
    account_login: str
    account_type: str
    status: str
    source: TenantSource


def parse_tenant_id(value: str | None) -> int | None:
    """Parse an optional positive integer tenant id."""
    if value is None or not value.strip():
        return None
    try:
        tenant_id = int(value.strip())
    except ValueError as exc:
        raise TenantResolutionError("invalid tenant id") from exc
    if tenant_id <= 0:
        raise TenantResolutionError("invalid tenant id")
    return tenant_id


def tenant_context_from_installation(
    installation: InstallationRecord,
    *,
    source: TenantSource,
) -> TenantContext:
    name = installation.account_login or f"installation-{installation.id}"
    return TenantContext(
        id=installation.id,
        name=name,
        github_installation_id=installation.github_installation_id,
        account_login=installation.account_login,
        account_type=installation.account_type,
        status=installation.status,
        source=source,
    )
