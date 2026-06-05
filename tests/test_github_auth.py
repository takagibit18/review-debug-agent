"""Tests for GitHub token and GitHub App authentication providers."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.integrations.github_auth import (
    GitHubAppAuthProvider,
    GitHubAuthError,
    InstallationToken,
    TokenAuthProvider,
    encode_github_app_jwt,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 201) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeInstallationTokenClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.closed = False
        self.next_token = "installation-token"

    async def post(self, path: str, *, headers: dict[str, str], json: dict[str, object]):  # type: ignore[no-untyped-def]
        self.posts.append({"path": path, "headers": headers, "json": json})
        return FakeResponse(
            {
                "token": self.next_token,
                "expires_at": "2026-06-04T13:00:00Z",
            }
        )

    async def aclose(self) -> None:
        self.closed = True


def test_token_auth_provider_uses_existing_github_token_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_existing")

    token = asyncio.run(TokenAuthProvider().get_token())

    assert token == "ghs_existing"


def test_github_app_auth_provider_creates_and_caches_installation_token() -> None:
    client = FakeInstallationTokenClient()
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    provider = GitHubAppAuthProvider(
        Settings(
            github_app_id="123",
            github_private_key="-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
        ),
        client=client,
        jwt_factory=lambda app_id, private_key, current_time: f"jwt-{app_id}",
        now=lambda: now,
    )

    first = asyncio.run(provider.get_token(installation_id=456))
    second = asyncio.run(provider.get_token(installation_id=456))

    assert first == "installation-token"
    assert second == "installation-token"
    assert len(client.posts) == 1
    assert client.posts[0]["path"] == "/app/installations/456/access_tokens"
    assert client.posts[0]["headers"] == {"Authorization": "Bearer jwt-123"}


def test_github_app_auth_provider_caches_tokens_per_installation() -> None:
    client = FakeInstallationTokenClient()
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    provider = GitHubAppAuthProvider(
        Settings(github_app_id="123", github_private_key="private-key"),
        client=client,
        jwt_factory=lambda app_id, private_key, current_time: "jwt",
        now=lambda: now,
    )

    client.next_token = "token-one"
    first = asyncio.run(provider.get_token(installation_id=1))
    client.next_token = "token-two"
    second = asyncio.run(provider.get_token(installation_id=2))
    client.next_token = "unused"
    first_again = asyncio.run(provider.get_token(installation_id=1))

    assert first == "token-one"
    assert second == "token-two"
    assert first_again == "token-one"
    assert [post["path"] for post in client.posts] == [
        "/app/installations/1/access_tokens",
        "/app/installations/2/access_tokens",
    ]


def test_github_app_auth_provider_refreshes_expiring_cached_token() -> None:
    client = FakeInstallationTokenClient()
    now = datetime(2026, 6, 4, 12, 56, tzinfo=UTC)
    provider = GitHubAppAuthProvider(
        Settings(github_app_id="123", github_private_key="private-key"),
        client=client,
        jwt_factory=lambda app_id, private_key, current_time: "jwt",
        now=lambda: now,
    )

    provider._cache[456] = InstallationToken(  # noqa: SLF001
        token="old-token",
        expires_at=now + timedelta(minutes=4),
    )

    token = asyncio.run(provider.get_token(installation_id=456))

    assert token == "installation-token"
    assert len(client.posts) == 1


def test_github_app_auth_provider_requires_installation_id() -> None:
    provider = GitHubAppAuthProvider(
        Settings(github_app_id="123", github_private_key="private-key"),
        jwt_factory=lambda app_id, private_key, current_time: "jwt",
    )

    with pytest.raises(GitHubAuthError, match="installation_id is required"):
        asyncio.run(provider.get_token())


def test_github_app_auth_provider_requires_app_id_and_private_key() -> None:
    missing_app_id = GitHubAppAuthProvider(
        Settings(github_app_id="", github_private_key="private-key"),
        jwt_factory=lambda app_id, private_key, current_time: "jwt",
    )
    missing_private_key = GitHubAppAuthProvider(
        Settings(github_app_id="123", github_private_key=""),
        jwt_factory=lambda app_id, private_key, current_time: "jwt",
    )

    with pytest.raises(GitHubAuthError, match="GITHUB_APP_ID is required"):
        asyncio.run(missing_app_id.get_token(installation_id=1))
    with pytest.raises(GitHubAuthError, match="GITHUB_PRIVATE_KEY is required"):
        asyncio.run(missing_private_key.get_token(installation_id=1))


def test_encode_github_app_jwt_uses_rs256_and_github_payload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def _encode(payload, private_key, algorithm):  # type: ignore[no-untyped-def]
        captured["payload"] = payload
        captured["private_key"] = private_key
        captured["algorithm"] = algorithm
        return "encoded-jwt"

    monkeypatch.setitem(sys.modules, "jwt", SimpleNamespace(encode=_encode))
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    token = encode_github_app_jwt("123", "private-key", now)

    assert token == "encoded-jwt"
    assert captured["private_key"] == "private-key"
    assert captured["algorithm"] == "RS256"
    payload = captured["payload"]
    assert payload["iss"] == "123"  # type: ignore[index]
    assert payload["iat"] == int((now - timedelta(seconds=60)).timestamp())  # type: ignore[index]
    assert payload["exp"] == int((now + timedelta(minutes=9)).timestamp())  # type: ignore[index]


def test_settings_normalizes_github_private_key_newlines() -> None:
    settings = Settings(github_private_key="-----BEGIN-----\\nabc\\n-----END-----")

    assert settings.github_private_key == "-----BEGIN-----\nabc\n-----END-----"
