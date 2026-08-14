"""Tests for model client provider-specific request controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.models.exceptions import ModelClientError, ModelTimeoutError
from src.models.client import ModelClient
from src.models.compat import ModelCallPolicy
from src.models.conversation import AssistantToolTurn, ModelConversation
from src.models.schemas import Message, ModelConfig


class _FakeCompletions:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.payload: dict[str, Any] | None = None
        self.delay_seconds = delay_seconds

    async def create(self, **payload: Any) -> Any:
        self.payload = payload
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
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.completions = _FakeCompletions(delay_seconds=delay_seconds)
        self.chat = SimpleNamespace(completions=self.completions)


def _make_client(
    fake: _FakeOpenAIClient,
    *,
    base_url: str = "https://api.example.test/v1",
) -> ModelClient:
    client = ModelClient.__new__(ModelClient)
    client._client = fake  # noqa: SLF001
    client._settings = SimpleNamespace(openai_base_url=base_url)  # noqa: SLF001
    client._default_config = ModelConfig(model="fake-model")  # noqa: SLF001
    client._max_retries = 1  # noqa: SLF001
    return client


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
