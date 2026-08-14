"""DeepSeek thinking and multi-turn tool protocol regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from src.models.client import ModelClient
from src.models.compat import ModelCallPolicy
from src.models.conversation import ModelConversation
from src.models.schemas import Message, ModelConfig


class _SequencedCompletions:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.payloads.append(payload)
        index = len(self.payloads)
        if index < 3:
            tool_calls = [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"round":{index}}}',
                    },
                }
            ]
            reasoning = f"reasoning-{index}"
        else:
            tool_calls = [
                {
                    "id": "submit-call",
                    "type": "function",
                    "function": {
                        "name": "submit_review",
                        "arguments": '{"summary":"done","issues":[]}',
                    },
                }
            ]
            reasoning = ""
        message = SimpleNamespace(
            content="",
            reasoning_content=reasoning,
            tool_calls=tool_calls,
        )
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
        )
        return SimpleNamespace(
            id=f"response-{index}",
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
            usage=usage,
            model="deepseek-v4",
        )


def _client(completions: _SequencedCompletions) -> ModelClient:
    client = ModelClient.__new__(ModelClient)
    client._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=completions)
    )
    client._settings = SimpleNamespace(  # noqa: SLF001
        model_provider="deepseek",
        openai_base_url="https://provider.example/v1",
    )
    client._default_config = ModelConfig(model="deepseek-v4")  # noqa: SLF001
    client._max_retries = 1  # noqa: SLF001
    return client


def test_thinking_tool_rounds_replay_then_finalize_forces_submit() -> None:
    completions = _SequencedCompletions()
    client = _client(completions)
    conversation = ModelConversation()
    base = [Message(role="user", content="review")]

    first = asyncio.run(
        client.chat(
            [*base, *conversation.messages()],
            policy=ModelCallPolicy(thinking="high"),
            conversation=conversation,
        )
    )
    conversation.add_tool_result("call-1", {"ok": True, "content": "first"})
    second = asyncio.run(
        client.chat(
            [*base, *conversation.messages()],
            policy=ModelCallPolicy(thinking="high"),
            conversation=conversation,
        )
    )
    conversation.add_tool_result("call-2", {"ok": True, "content": "second"})
    final = asyncio.run(
        client.chat(
            [*base, *conversation.messages()],
            policy=ModelCallPolicy(
                thinking="off",
                forced_tool="submit_review",
            ),
            conversation=conversation,
        )
    )

    assert first.usage.reasoning_tokens == 12
    assert second.usage.reasoning_tokens == 12
    assert final.usage.reasoning_tokens == 12
    assert completions.payloads[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert completions.payloads[0]["reasoning_effort"] == "high"
    second_messages = completions.payloads[1]["messages"]
    assert second_messages[1]["reasoning_content"] == "reasoning-1"
    assert [message["role"] for message in second_messages[1:]] == [
        "assistant",
        "tool",
    ]

    final_payload = completions.payloads[2]
    assert final_payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in final_payload
    assert final_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
    replay = final_payload["messages"][1:]
    assert [message["role"] for message in replay] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert replay[0]["reasoning_content"] == "reasoning-1"
    assert replay[2]["reasoning_content"] == "reasoning-2"
    assert replay[0]["content"] == ""
    assert replay[2]["content"] == ""


def test_non_deepseek_high_policy_does_not_add_provider_controls() -> None:
    completions = _SequencedCompletions()
    client = _client(completions)
    client._settings.model_provider = "openai"  # noqa: SLF001

    asyncio.run(
        client.chat(
            [Message(role="user", content="review")],
            policy=ModelCallPolicy(thinking="high"),
        )
    )

    payload = completions.payloads[0]
    assert "extra_body" not in payload
    assert "reasoning_effort" not in payload
