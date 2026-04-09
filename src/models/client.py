"""OpenAI-compatible model client.

Thin async wrapper around the ``openai`` SDK that handles authentication,
retries, and token-usage tracking.
"""

from __future__ import annotations

import asyncio
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError as OpenAIAuthenticationError,
    RateLimitError as OpenAIRateLimitError,
)

from src.config import Settings, get_settings
from src.models.exceptions import (
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.models.schemas import Message, ModelConfig, ModelResponse, TokenUsage


class ModelClient:
    """Async OpenAI-compatible client with retries and usage tracking."""

    def __init__(
        self, settings: Settings | None = None, *, max_retries: int = 3
    ) -> None:
        self._settings = settings or get_settings()
        if not self._settings.openai_api_key:
            raise AuthenticationError("OPENAI_API_KEY is empty or missing")

        self._client = AsyncOpenAI(
            api_key=self._settings.openai_api_key,
            base_url=str(self._settings.openai_base_url),
        )
        self._default_config = ModelConfig(model=self._settings.model_name)
        self._max_retries = max(1, max_retries)

    async def chat(
        self,
        messages: list[Message],
        config: ModelConfig | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Run one chat-completion request and return normalized output."""
        if not messages:
            raise ModelClientError("messages must not be empty")

        runtime_config = config or self._default_config
        payload: dict[str, Any] = {
            "model": runtime_config.model,
            "messages": self._serialize_messages(messages),
            "temperature": runtime_config.temperature,
            "max_tokens": runtime_config.max_tokens,
            "top_p": runtime_config.top_p,
        }
        if tools:
            payload["tools"] = tools

        last_error: ModelClientError | None = None
        for attempt in range(self._max_retries):
            try:
                completion = await self._client.chat.completions.create(
                    **payload,
                    timeout=runtime_config.timeout,
                )
                return self._parse_completion(completion)
            except OpenAIAuthenticationError as exc:
                raise AuthenticationError(
                    "Authentication failed for the model provider",
                    status_code=401,
                    code="auth_failed",
                ) from exc
            except OpenAIRateLimitError:
                last_error = RateLimitError(
                    "Rate limit reached while calling model provider",
                    status_code=429,
                    code="rate_limited",
                )
            except APITimeoutError:
                last_error = ModelTimeoutError(
                    "Model provider request timed out",
                    code="timeout",
                )
            except APIStatusError as exc:
                if exc.status_code in {401, 403}:
                    raise AuthenticationError(
                        "Authentication failed for the model provider",
                        status_code=exc.status_code,
                        code="auth_failed",
                    ) from exc
                if exc.status_code == 429:
                    last_error = RateLimitError(
                        "Rate limit reached while calling model provider",
                        status_code=exc.status_code,
                        code="rate_limited",
                    )
                elif exc.status_code >= 500:
                    last_error = ServiceUnavailableError(
                        "Model provider is temporarily unavailable",
                        status_code=exc.status_code,
                        code="provider_unavailable",
                    )
                else:
                    raise ModelClientError(
                        "Model provider returned a non-retriable status",
                        status_code=exc.status_code,
                        code="api_status_error",
                    ) from exc
            except APIConnectionError:
                last_error = ServiceUnavailableError(
                    "Failed to connect to the model provider",
                    code="connection_error",
                )
            except Exception as exc:  # noqa: BLE001
                raise ModelClientError(
                    "Unexpected model client error",
                    code="unexpected_error",
                ) from exc

            if attempt < self._max_retries - 1 and last_error is not None:
                await asyncio.sleep(2**attempt)
                continue

            if last_error is not None:
                raise last_error

        raise ModelClientError("Model request failed after retries", code="max_retries")

    async def close(self) -> None:
        """Close underlying HTTP resources."""
        await self._client.close()

    @staticmethod
    def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = message.tool_calls
            serialized.append(item)
        return serialized

    @staticmethod
    def _parse_completion(completion: Any) -> ModelResponse:
        choice = completion.choices[0] if completion.choices else None
        response_message = choice.message if choice else None

        content = response_message.content if response_message else ""
        if content is None:
            content = ""

        tool_calls: list[dict[str, Any]] = []
        if response_message and response_message.tool_calls:
            for tool_call in response_message.tool_calls:
                if hasattr(tool_call, "model_dump"):
                    tool_calls.append(tool_call.model_dump())
                elif isinstance(tool_call, dict):
                    tool_calls.append(tool_call)

        usage = completion.usage
        token_usage = TokenUsage(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        )

        finish_reason = ""
        if choice and choice.finish_reason:
            finish_reason = str(choice.finish_reason)

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=token_usage,
            model=str(getattr(completion, "model", "") or ""),
            finish_reason=finish_reason,
        )
