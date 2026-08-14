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
from src.models.compat import ModelCallPolicy, ModelProfile, resolve_model_profile
from src.models.schemas import Message, ModelConfig, ModelResponse, TokenUsage


class ModelClient:
    """Async OpenAI-compatible client with retries and usage tracking."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_retries: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if not self._settings.openai_api_key:
            raise AuthenticationError("OPENAI_API_KEY is empty or missing")

        self._client = AsyncOpenAI(
            api_key=self._settings.openai_api_key,
            base_url=str(self._settings.openai_base_url),
        )
        default_config_kwargs: dict[str, Any] = {
            "model": self._settings.model_name,
            "max_tokens": self._settings.model_max_tokens,
            "timeout": self._settings.model_request_timeout_seconds,
        }
        if temperature is not None:
            default_config_kwargs["temperature"] = temperature
        self._default_config = ModelConfig(**default_config_kwargs)
        self._max_retries = max(1, max_retries if max_retries is not None else self._settings.model_max_retries)

    @property
    def default_config(self) -> ModelConfig:
        """Return an immutable copy of default runtime config."""
        return self._default_config.model_copy(deep=True)

    async def chat(
        self,
        messages: list[Message],
        config: ModelConfig | None = None,
        tools: list[dict[str, Any]] | None = None,
        policy: ModelCallPolicy | None = None,
    ) -> ModelResponse:
        """Run one chat-completion request and return normalized output."""
        if not messages:
            raise ModelClientError("messages must not be empty")

        source_config = config or self._default_config
        profile = self.profile_for(source_config.model)
        runtime_config, runtime_policy = self._apply_policy(
            source_config, policy, profile
        )
        payload: dict[str, Any] = {
            "model": runtime_config.model,
            "messages": self._serialize_messages(messages),
            "temperature": runtime_config.temperature,
            "max_tokens": runtime_config.max_tokens,
            "top_p": runtime_config.top_p,
        }
        if tools:
            payload["tools"] = tools
        if runtime_config.tool_choice is not None:
            payload["tool_choice"] = runtime_config.tool_choice
        if runtime_config.extra_body is not None:
            payload["extra_body"] = runtime_config.extra_body
        if (
            runtime_policy.thinking == "high"
            and profile.compat.supports_reasoning_effort
        ):
            payload["reasoning_effort"] = "high"

        last_error: ModelClientError | None = None
        for attempt in range(self._max_retries):
            try:
                completion = await asyncio.wait_for(
                    self._client.chat.completions.create(
                        **payload,
                        timeout=runtime_config.timeout,
                    ),
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
            except (APITimeoutError, asyncio.TimeoutError):
                last_error = ModelTimeoutError(
                    f"Model provider request timed out after {runtime_config.timeout:g}s",
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
                    body_preview = ""
                    try:
                        body = exc.response.content if exc.response else b""
                        body_preview = body.decode("utf-8", errors="replace")[:500]
                    except Exception:  # noqa: BLE001
                        pass
                    raise ModelClientError(
                        f"Model provider returned status {exc.status_code}"
                        + (f": {body_preview}" if body_preview else ""),
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

    def profile_for(self, model: str) -> ModelProfile:
        """Resolve compatibility metadata for a configured model."""

        return resolve_model_profile(self._settings, model)

    @classmethod
    def _apply_policy(
        cls,
        config: ModelConfig,
        policy: ModelCallPolicy | None,
        profile: ModelProfile,
    ) -> tuple[ModelConfig, ModelCallPolicy]:
        """Translate provider-neutral call intent into current wire controls."""

        forced_from_config = cls._forced_tool_name(config.tool_choice)
        if policy is None and forced_from_config is None:
            return config, ModelCallPolicy(thinking="off")
        effective = policy or ModelCallPolicy(
            thinking="off",
            forced_tool=forced_from_config,
        )
        if (
            effective.thinking != "off"
            and effective.forced_tool
            and not profile.compat.supports_tool_choice_with_thinking
        ):
            raise ModelClientError(
                "Model profile does not support forced tool choice with thinking",
                code="incompatible_call_policy",
            )

        updated = config.model_copy(deep=True)
        if effective.forced_tool:
            updated.tool_choice = {
                "type": "function",
                "function": {"name": effective.forced_tool},
            }
        if profile.compat.thinking_format == "deepseek":
            updated.extra_body = {
                **(updated.extra_body or {}),
                "thinking": {
                    "type": "enabled" if effective.thinking == "high" else "disabled"
                },
            }
        elif profile.compat.thinking_format == "dashscope":
            updated.extra_body = {
                **(updated.extra_body or {}),
                "enable_thinking": effective.thinking == "high",
            }
        return updated, effective

    @staticmethod
    def _is_forced_tool_choice(value: str | dict[str, Any] | None) -> bool:
        if isinstance(value, dict):
            return value.get("type") == "function"
        return value == "required"

    @staticmethod
    def _forced_tool_name(value: str | dict[str, Any] | None) -> str | None:
        if not isinstance(value, dict) or value.get("type") != "function":
            return None
        function = value.get("function")
        if not isinstance(function, dict):
            return None
        name = str(function.get("name", "")).strip()
        return name or None

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
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = message.tool_calls
            if message.reasoning_content is not None:
                item["reasoning_content"] = message.reasoning_content
            serialized.append(item)
        return serialized

    @staticmethod
    def _parse_completion(completion: Any) -> ModelResponse:
        choice = completion.choices[0] if completion.choices else None
        response_message = choice.message if choice else None

        content = response_message.content if response_message else ""
        if content is None:
            content = ""

        reasoning = ""
        if response_message:
            raw_reasoning = getattr(response_message, "reasoning_content", None)
            if isinstance(raw_reasoning, str):
                reasoning = raw_reasoning

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
            reasoning_content=reasoning,
        )
