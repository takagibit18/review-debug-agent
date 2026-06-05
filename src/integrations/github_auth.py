"""GitHub API authentication providers for token and GitHub App modes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.integrations.github_publisher import resolve_github_token

logger = logging.getLogger(__name__)


class GitHubAuthError(RuntimeError):
    """Raised when GitHub authentication cannot produce an API token."""


class GitHubAuthProvider(Protocol):
    """Resolve a GitHub API token for one request context."""

    async def get_token(self, installation_id: int | None = None) -> str: ...


class InstallationToken(BaseModel):
    """Short-lived installation token cache entry."""

    token: str = Field(min_length=1)
    expires_at: datetime


JwtFactory = Callable[[str, str, datetime], str]


class TokenAuthProvider:
    """Use the existing GitHub token environment variables."""

    def __init__(self, explicit_token: str | None = None) -> None:
        self._explicit_token = explicit_token

    async def get_token(self, installation_id: int | None = None) -> str:
        token = resolve_github_token(self._explicit_token)
        if not token:
            raise GitHubAuthError("GITHUB_TOKEN/GH_TOKEN is required in token auth mode.")
        return token


class GitHubAppAuthProvider:
    """Exchange a GitHub App JWT for installation access tokens."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        jwt_factory: JwtFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._jwt_factory = jwt_factory or encode_github_app_jwt
        self._now = now or (lambda: datetime.now(UTC))
        self._cache: dict[int, InstallationToken] = {}

    async def get_token(self, installation_id: int | None = None) -> str:
        if installation_id is None:
            raise GitHubAuthError("installation_id is required in GitHub App auth mode.")
        cached = self._cache.get(installation_id)
        if cached and cached.expires_at - self._now() > timedelta(minutes=5):
            return cached.token

        app_jwt = self._build_jwt()
        token = await self._create_installation_token(installation_id, app_jwt)
        self._cache[installation_id] = token
        logger.info(
            "github installation token created",
            extra={
                "installation_id": installation_id,
                "expires_at": token.expires_at.isoformat(),
            },
        )
        return token.token

    def _build_jwt(self) -> str:
        app_id = self._settings.github_app_id
        private_key = self._settings.github_private_key
        if not app_id:
            raise GitHubAuthError("GITHUB_APP_ID is required in GitHub App auth mode.")
        if not private_key:
            raise GitHubAuthError("GITHUB_PRIVATE_KEY is required in GitHub App auth mode.")
        return self._jwt_factory(app_id, private_key, self._now())

    async def _create_installation_token(
        self,
        installation_id: int,
        app_jwt: str,
    ) -> InstallationToken:
        client_created = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        try:
            response = await client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}"},
                json={},
            )
            _raise_github_auth_error(response, f"/app/installations/{installation_id}/access_tokens")
            data = response.json()
            raw_token = str(data.get("token", "") or "").strip()
            raw_expires_at = str(data.get("expires_at", "") or "").strip()
            if not raw_token or not raw_expires_at:
                raise GitHubAuthError("GitHub installation token response was missing token/expires_at.")
            return InstallationToken(
                token=raw_token,
                expires_at=_parse_github_datetime(raw_expires_at),
            )
        finally:
            if client_created:
                await client.aclose()


def get_github_auth_provider(
    settings: Settings | None = None,
    *,
    explicit_token: str | None = None,
) -> GitHubAuthProvider:
    """Build the configured GitHub auth provider without leaking mode conditionals."""
    resolved = settings or get_settings()
    if resolved.github_auth_mode == "app":
        return GitHubAppAuthProvider(resolved)
    return TokenAuthProvider(explicit_token)


def encode_github_app_jwt(app_id: str, private_key: str, now: datetime) -> str:
    """Create a GitHub App JWT signed with RS256."""
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover - exercised only without dependency in app mode
        raise GitHubAuthError(
            "PyJWT[crypto] is required for GitHub App auth. Install requirements.txt."
        ) from exc

    issued_at = int((now - timedelta(seconds=60)).timestamp())
    expires_at = int((now + timedelta(minutes=9)).timestamp())
    payload = {
        "iat": issued_at,
        "exp": expires_at,
        "iss": app_id,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    return token.decode("utf-8") if isinstance(token, bytes) else str(token)


def _parse_github_datetime(raw: str) -> datetime:
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _raise_github_auth_error(response: Any, path: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status_code < 300:
        return
    text = str(getattr(response, "text", "") or "")[:400].replace("\n", " ")
    raise GitHubAuthError(
        f"GitHub API {status_code} while creating installation token for {path}. "
        f"Body preview: {text!r}"
    )
