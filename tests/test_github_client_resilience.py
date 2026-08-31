"""Deterministic fault injection for the GitHub REST retry boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from src.integrations.github_publisher import GitHubApiClient


def test_get_retries_503_503_then_returns_200() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"number": 7})

    result, sleeps = _run_get(handler)

    assert result == {"number": 7}
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


def test_get_respects_retry_after_on_429() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2.25"},
                json={"message": "slow down"},
            )
        return httpx.Response(200, json={"number": 7})

    result, sleeps = _run_get(handler)

    assert result == {"number": 7}
    assert attempts == 2
    assert sleeps == [2.25]


def test_403_fails_without_retry() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"message": "forbidden"})

    async def scenario() -> list[float]:
        client, sleeps = _client(handler)
        try:
            with pytest.raises(RuntimeError, match="GitHub API 403"):
                await client.get_pull_request("owner/repo", 7)
        finally:
            await client.close()
        return sleeps

    sleeps = asyncio.run(scenario())

    assert attempts == 1
    assert sleeps == []


def test_post_timeout_is_not_blindly_retried() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response was lost", request=request)

    async def scenario() -> list[float]:
        client, sleeps = _client(handler)
        try:
            with pytest.raises(RuntimeError, match="POST.*1 attempt"):
                await client.create_review_comment(
                    "owner/repo",
                    7,
                    {"body": "advisory"},
                )
        finally:
            await client.close()
        return sleeps

    sleeps = asyncio.run(scenario())

    assert attempts == 1
    assert sleeps == []


def test_get_connection_error_recovers_with_bounded_retry() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"number": 7})

    result, sleeps = _run_get(handler)

    assert result == {"number": 7}
    assert attempts == 2
    assert sleeps == [0.5]


def test_patch_to_known_resource_can_retry() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "PATCH"
        if attempts == 1:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(200, json={"id": 42})

    async def scenario() -> tuple[dict[str, Any], list[float]]:
        client, sleeps = _client(handler)
        try:
            result = await client.update_review_comment("owner/repo", 42, "updated")
        finally:
            await client.close()
        return result, sleeps

    result, sleeps = asyncio.run(scenario())

    assert result == {"id": 42}
    assert attempts == 2
    assert sleeps == [0.5]


def _run_get(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> tuple[dict[str, Any], list[float]]:
    async def scenario() -> tuple[dict[str, Any], list[float]]:
        client, sleeps = _client(handler)
        try:
            result = await client.get_pull_request("owner/repo", 7)
        finally:
            await client.close()
        return result, sleeps

    return asyncio.run(scenario())


def _client(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> tuple[GitHubApiClient, list[float]]:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    return (
        GitHubApiClient(
            "offline-token",
            base_url="https://api.github.test",
            max_attempts=3,
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
            random_source=lambda: 0.0,
        ),
        sleeps,
    )
