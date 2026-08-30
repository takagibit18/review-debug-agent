"""Tests for model client provider-specific request controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError as OpenAIAuthenticationError,
    RateLimitError as OpenAIRateLimitError,
)

try:
    import httpx2 as sdk_httpx
except ImportError:  # pragma: no cover - compatibility with openai<3
    import httpx as sdk_httpx

from src.models.client import ModelClient
from src.models.compat import ModelCallPolicy
from src.models.conversation import AssistantToolTurn, ModelConversation
from src.models.exceptions import (
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
)
from src.models.schemas import Message, ModelConfig


class _FakeCompletions:
    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        failures: list[BaseException] | None = None,
    ) -> None:
        self.payload: dict[str, Any] | None = None
        self.delay_seconds = delay_seconds
        self.failures = list(failures or [])
        self.calls = 0

    async def create(self, **payload: Any) -> Any:
        self.calls += 1
        self.payload = payload
        if self.failures:
            raise self.failures.pop(0)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        message = SimpleNamespace(
            content="ok",
            reasoning_content="kept reasoning",
            tool_calls=None,
        )
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
        )
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model="fake-model",
        )


class _FakeOpenAIClient:
    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        failures: list[BaseException] | None = None,
    ) -> None:
        self.completions = _FakeCompletions(
            delay_seconds=delay_seconds,
            failures=failures,
        )
        self.chat = SimpleNamespace(completions=self.completions)


def _make_client(
    fake: _FakeOpenAIClient,
    *,
    base_url: str = "https://api.example.test/v1",
    max_retries: int = 1,
) -> ModelClient:
    client = ModelClient.__new__(ModelClient)
    client._client = fake  # noqa: SLF001
    client._settings = SimpleNamespace(openai_base_url=base_url)  # noqa: SLF001
    client._default_config = ModelConfig(model="fake-model")  # noqa: SLF001
    client._max_retries = max_retries  # noqa: SLF001
    return client


_SDK_REQUEST = sdk_httpx.Request("POST", "https://api.example.test/v1/chat/completions")


@pytest.mark.parametrize(
    "transient_error",
    [
        APIStatusError(
            "unavailable",
            response=sdk_httpx.Response(
                503,
                request=_SDK_REQUEST,
                content=b"unavailable",
            ),
            body=None,
        ),
        OpenAIRateLimitError(
            "rate limited",
            response=sdk_httpx.Response(429, request=_SDK_REQUEST),
            body=None,
        ),
        APIConnectionError(request=_SDK_REQUEST),
        APITimeoutError(_SDK_REQUEST),
    ],
    ids=["503", "429", "connection", "timeout"],
)
def test_model_client_retries_classified_transient_failures(
    transient_error: BaseException,
    monkeypatch,
) -> None:
    fake = _FakeOpenAIClient(failures=[transient_error])
    client = _make_client(fake, max_retries=2)
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("src.models.client.asyncio.sleep", record_sleep)

    response = asyncio.run(client.chat(messages=[Message(role="user", content="retry")]))

    assert response.content == "ok"
    assert fake.completions.calls == 2
    assert sleeps == [1]


def test_model_client_authentication_error_fails_without_retry(monkeypatch) -> None:
    failure = OpenAIAuthenticationError(
        "unauthorized",
        response=sdk_httpx.Response(401, request=_SDK_REQUEST),
        body=None,
    )
    fake = _FakeOpenAIClient(failures=[failure])
    client = _make_client(fake, max_retries=3)
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("src.models.client.asyncio.sleep", record_sleep)

    with pytest.raises(AuthenticationError):
        asyncio.run(client.chat(messages=[Message(role="user", content="fail")]))

    assert fake.completions.calls == 1
    assert sleeps == []


def test_forced_tool_choice_disables_deepseek_thinking_without_mutating_config() -> (
    None
):
    fake = _FakeOpenAIClient()
    client = _make_client(fake, base_url="https://api.deepseek.com/v1")
    config = ModelConfig(
        model="deepseek-v4-pro",
        tool_choice={"type": "function", "function": {"name": "verify"}},
        extra_body={"trace_id": "keep-me"},
    )

    asyncio.run(
        client.chat(messages=[Message(role="user", content="verify")], config=config)
    )

    assert fake.completions.payload is not None
    assert fake.completions.payload["extra_body"] == {
        "trace_id": "keep-me",
        "thinking": {"type": "disabled"},
    }
    assert config.extra_body == {"trace_id": "keep-me"}


def test_forced_tool_choice_disables_dashscope_thinking() -> None:
    fake = _FakeOpenAIClient()
    client = _make_client(
        fake, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    config = ModelConfig(
        model="qwen-plus",
        tool_choice={"type": "function", "function": {"name": "verify"}},
    )

    asyncio.run(
        client.chat(messages=[Message(role="user", content="verify")], config=config)
    )

    assert fake.completions.payload is not None
    assert fake.completions.payload["extra_body"] == {"enable_thinking": False}


def test_call_without_forced_tool_choice_does_not_change_thinking() -> None:
    fake = _FakeOpenAIClient()
    client = _make_client(fake, base_url="https://api.deepseek.com/v1")

    asyncio.run(
        client.chat(
            messages=[Message(role="user", content="ordinary")],
            config=ModelConfig(model="deepseek-v4-pro"),
        )
    )

    assert fake.completions.payload is not None
    assert "extra_body" not in fake.completions.payload


def test_chat_forwards_tool_choice_extra_body_and_transient_thinking() -> None:
    fake = _FakeOpenAIClient()
    client = _make_client(fake)
    config = ModelConfig(
        model="deepseek-v4-pro",
        max_tokens=8192,
        tool_choice={"type": "function", "function": {"name": "submit_review"}},
        extra_body={"thinking": {"type": "disabled"}},
    )

    conversation = ModelConversation()
    conversation.add_assistant_tool_turn(
        response_id="prior",
        content="",
        thinking="prior reasoning",
        tool_calls=[{"id": "call-1", "function": {"name": "read_file"}}],
    )
    conversation.add_tool_result("call-1", {"ok": True})

    response = asyncio.run(
        client.chat(
            messages=conversation.messages(),
            config=config,
            tools=[{"type": "function", "function": {"name": "submit_review"}}],
            conversation=conversation,
        )
    )

    payload = fake.completions.payload
    assert payload is not None
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
    assert payload["max_tokens"] == 8192
    assert payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert payload["messages"][0]["reasoning_content"] == "prior reasoning"
    assert isinstance(conversation.turns[0], AssistantToolTurn)
    assert response.usage.reasoning_tokens == 0


def test_chat_enforces_outer_request_timeout() -> None:
    fake = _FakeOpenAIClient(delay_seconds=0.05)
    client = _make_client(fake)

    with pytest.raises(ModelTimeoutError):
        asyncio.run(
            client.chat(
                messages=[Message(role="user", content="slow")],
                config=ModelConfig(model="fake-model", timeout=0.01),
            )
        )


def test_explicit_provider_overrides_legacy_url_detection() -> None:
    fake = _FakeOpenAIClient()
    client = _make_client(fake, base_url="https://api.deepseek.com/v1")
    client._settings.model_provider = "openai"  # noqa: SLF001

    asyncio.run(
        client.chat(
            messages=[Message(role="user", content="ordinary")],
            config=ModelConfig(model="deepseek-v4-pro"),
            policy=ModelCallPolicy(thinking="off"),
        )
    )

    assert fake.completions.payload is not None
    assert "extra_body" not in fake.completions.payload


def test_deepseek_rejects_thinking_with_forced_tool() -> None:
    fake = _FakeOpenAIClient()
    client = _make_client(fake, base_url="https://api.deepseek.com/v1")

    with pytest.raises(ModelClientError, match="forced tool choice with thinking"):
        asyncio.run(
            client.chat(
                messages=[Message(role="user", content="verify")],
                config=ModelConfig(model="deepseek-v4-pro"),
                policy=ModelCallPolicy(thinking="high", forced_tool="verify"),
            )
        )
