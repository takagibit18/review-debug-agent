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
from src.models.conversation import ModelConversation
from src.models.schemas import Message, ModelConfig, ModelResponse, TokenUsage
from src.models.token_telemetry import (
    common_prefix_tokens,
    component_hash,
    estimate_tokens,
    serialize_json,
)


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
        self._max_retries = max(
            1,
            max_retries
            if max_retries is not None
            else self._settings.model_max_retries,
        )
        self._last_call_attempts: list[dict[str, Any]] = []
        self._last_request_text = ""
        self._request_telemetry: dict[str, Any] = {}

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
        conversation: ModelConversation | None = None,
    ) -> ModelResponse:
        """Run one chat-completion request and return normalized output."""
        if not messages:
            raise ModelClientError("messages must not be empty")

        # A chat() call is one logical model call.  The list is retained until
        # the inference layer consumes it so retries remain individually visible.
        self._last_call_attempts = []

        source_config = config or self._default_config
        profile = self.profile_for(source_config.model)
        runtime_config, runtime_policy = self._apply_policy(
            source_config, policy, profile
        )
        payload: dict[str, Any] = {
            "model": runtime_config.model,
            "messages": self._serialize_messages(messages, profile),
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
            payload["reasoning_effort"] = (
                "low"
                if (
                    profile.compat.thinking_format == "zhipu"
                    and not profile.compat.supports_thinking_disable
                )
                else "high"
            )
        elif (
            runtime_policy.thinking == "off"
            and profile.compat.thinking_format == "zhipu"
            and not profile.compat.supports_thinking_disable
            and profile.compat.supports_reasoning_effort
        ):
            # GLM-5.3/Flash must keep thinking enabled. ``low`` is the
            # provider-accepted lower bound for that model family.
            payload["reasoning_effort"] = "low"

        actual_reasoning_effort = self._wire_reasoning_effort(payload)
        request_text = serialize_json(
            {
                "model": payload.get("model"),
                "messages": payload.get("messages", []),
                "tools": payload.get("tools", []),
                "tool_choice": payload.get("tool_choice"),
                "extra_body": payload.get("extra_body"),
                "reasoning_effort": payload.get("reasoning_effort"),
            }
        )
        previous_request_text = getattr(self, "_last_request_text", "")
        common_tokens = common_prefix_tokens(previous_request_text, request_text)
        common_chars = 0
        for left, right in zip(
            previous_request_text, request_text, strict=False
        ):
            if left != right:
                break
            common_chars += 1
        self._request_telemetry = {
            "request_hash": component_hash(request_text),
            "request_estimated_tokens": estimate_tokens(request_text),
            "adjacent_common_prefix_tokens": common_tokens,
            "adjacent_prefix_hash": component_hash(request_text[:common_chars]),
        }
        self._last_request_text = request_text

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
                response = self._parse_completion(completion)
                response.provider_request_id = str(
                    self._field_value(completion, "id", "") or ""
                )
                response.actual_reasoning_effort = actual_reasoning_effort
                self._last_call_attempts.append(
                    self._attempt_payload(
                        attempt=attempt + 1,
                        success=True,
                        runtime_policy=runtime_policy,
                        actual_reasoning_effort=actual_reasoning_effort,
                        tool_schema_count=len(tools or []),
                        usage=response.usage,
                        usage_present=response.usage_present,
                        provider_request_id=response.provider_request_id,
                    )
                )
                if conversation is not None and response.tool_calls:
                    conversation.add_assistant_tool_turn(
                        response_id=str(
                            self._field_value(completion, "id", "") or ""
                        ),
                        content=response.content,
                        thinking=self._extract_thinking(completion),
                        tool_calls=response.tool_calls,
                    )
                return response
            except OpenAIAuthenticationError as exc:
                error = AuthenticationError(
                    "Authentication failed for the model provider",
                    status_code=401,
                    code="auth_failed",
                )
                self._last_call_attempts.append(
                    self._attempt_payload(
                        attempt=attempt + 1,
                        success=False,
                        runtime_policy=runtime_policy,
                        actual_reasoning_effort=actual_reasoning_effort,
                        tool_schema_count=len(tools or []),
                        error=error,
                    )
                )
                raise error from exc
            except OpenAIRateLimitError:
                last_error = RateLimitError(
                    "Rate limit reached while calling model provider",
                    status_code=429,
                    code="rate_limited",
                )
            except APITimeoutError as exc:
                last_error = ModelTimeoutError(
                    f"Model provider request timed out after {runtime_config.timeout:g}s",
                    code=self._sdk_timeout_code(exc),
                )
            except asyncio.TimeoutError:
                last_error = ModelTimeoutError(
                    f"Model provider request timed out after {runtime_config.timeout:g}s",
                    code="application_request_timeout",
                )
            except APIStatusError as exc:
                if exc.status_code in {401, 403}:
                    error = AuthenticationError(
                        "Authentication failed for the model provider",
                        status_code=exc.status_code,
                        code="auth_failed",
                    )
                    self._last_call_attempts.append(
                        self._attempt_payload(
                            attempt=attempt + 1,
                            success=False,
                            runtime_policy=runtime_policy,
                            actual_reasoning_effort=actual_reasoning_effort,
                            tool_schema_count=len(tools or []),
                            error=error,
                        )
                    )
                    raise error from exc
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
                    error = ModelClientError(
                        f"Model provider returned status {exc.status_code}"
                        + (f": {body_preview}" if body_preview else ""),
                        status_code=exc.status_code,
                        code="api_status_error",
                    )
                    self._last_call_attempts.append(
                        self._attempt_payload(
                            attempt=attempt + 1,
                            success=False,
                            runtime_policy=runtime_policy,
                            actual_reasoning_effort=actual_reasoning_effort,
                            tool_schema_count=len(tools or []),
                            error=error,
                        )
                    )
                    raise error from exc
            except APIConnectionError:
                last_error = ServiceUnavailableError(
                    "Failed to connect to the model provider",
                    code="connection_error",
                )
            except Exception as exc:  # noqa: BLE001
                error = ModelClientError(
                    "Unexpected model client error",
                    code="unexpected_error",
                )
                self._last_call_attempts.append(
                    self._attempt_payload(
                        attempt=attempt + 1,
                        success=False,
                        runtime_policy=runtime_policy,
                        actual_reasoning_effort=actual_reasoning_effort,
                        tool_schema_count=len(tools or []),
                        error=error,
                    )
                )
                raise error from exc

            if last_error is not None:
                self._last_call_attempts.append(
                    self._attempt_payload(
                        attempt=attempt + 1,
                        success=False,
                        runtime_policy=runtime_policy,
                        actual_reasoning_effort=actual_reasoning_effort,
                        tool_schema_count=len(tools or []),
                        error=last_error,
                    )
                )

            if attempt < self._max_retries - 1 and last_error is not None:
                await asyncio.sleep(2**attempt)
                continue

            if last_error is not None:
                raise last_error

        raise ModelClientError("Model request failed after retries", code="max_retries")

    def consume_call_telemetry(self) -> list[dict[str, Any]]:
        """Return and clear the attempt records for the last logical chat call."""

        attempts = list(getattr(self, "_last_call_attempts", []))
        self._last_call_attempts = []
        return attempts

    def profile_for(self, model: str) -> ModelProfile:
        """Resolve compatibility metadata for a configured model."""

        return resolve_model_profile(self._settings, model)

    @staticmethod
    def _sdk_timeout_code(exc: BaseException) -> str:
        """Classify an SDK timeout without depending on httpx internals."""

        current: BaseException | None = exc
        for _ in range(4):
            name = current.__class__.__name__.lower()
            if "connecttimeout" in name:
                return "sdk_connect_timeout"
            if "readtimeout" in name:
                return "sdk_read_timeout"
            current = current.__cause__ or current.__context__
            if current is None:
                break
        return "sdk_timeout"

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
        elif profile.compat.thinking_format == "zhipu":
            thinking_enabled = (
                effective.thinking == "high"
                or not profile.compat.supports_thinking_disable
            )
            updated.extra_body = {
                **(updated.extra_body or {}),
                "thinking": {
                    "type": "enabled" if thinking_enabled else "disabled"
                },
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
    def _wire_reasoning_effort(payload: dict[str, Any]) -> str:
        """Describe the provider control that was actually placed on the wire."""

        direct = payload.get("reasoning_effort")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        extra_body = payload.get("extra_body")
        if isinstance(extra_body, dict):
            thinking = extra_body.get("thinking")
            if isinstance(thinking, dict) and isinstance(thinking.get("type"), str):
                return thinking["type"]
            if isinstance(extra_body.get("enable_thinking"), bool):
                return "high" if extra_body["enable_thinking"] else "off"
        return "not_sent"

    def _attempt_payload(
        self,
        *,
        attempt: int,
        success: bool,
        runtime_policy: ModelCallPolicy,
        actual_reasoning_effort: str,
        tool_schema_count: int,
        usage: TokenUsage | None = None,
        usage_present: bool = False,
        provider_request_id: str = "",
        error: ModelClientError | None = None,
    ) -> dict[str, Any]:
        usage = usage or TokenUsage()
        payload: dict[str, Any] = {
            "provider_attempt": attempt,
            "thinking": runtime_policy.thinking,
            "actual_reasoning_effort": actual_reasoning_effort,
            "forced_tool": runtime_policy.forced_tool or "none",
            "tool_schema_count": tool_schema_count,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "provider_cache_hit": bool(
                usage.cached_prompt_tokens is not None
                and usage.cached_prompt_tokens > 0
            ),
            "usage_present": bool(usage_present),
            "success": success,
            "provider_request_id": provider_request_id,
            "usage_unknown": bool(not success and not usage_present),
        }
        if error is not None:
            payload.update(
                {
                    "failure_type": error.__class__.__name__,
                    "failure_status": error.status_code,
                    "provider_code": error.code or "",
                }
            )
        payload.update(getattr(self, "_request_telemetry", {}))
        return payload

    @staticmethod
    def _serialize_messages(
        messages: list[Message], profile: ModelProfile
    ) -> list[dict[str, Any]]:
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
            if (
                message.thinking is not None
                and profile.compat.requires_reasoning_replay_for_tool_calls
            ):
                item["reasoning_content"] = message.thinking
            serialized.append(item)
        return serialized

    @staticmethod
    def _parse_completion(completion: Any) -> ModelResponse:
        choices = ModelClient._field_value(completion, "choices", []) or []
        choice = choices[0] if choices else None
        response_message = ModelClient._field_value(choice, "message")

        content = ModelClient._field_value(response_message, "content", "")
        if content is None:
            content = ""

        tool_calls: list[dict[str, Any]] = []
        raw_tool_calls = ModelClient._field_value(response_message, "tool_calls", [])
        if raw_tool_calls:
            for tool_call in raw_tool_calls:
                if hasattr(tool_call, "model_dump"):
                    tool_calls.append(tool_call.model_dump())
                elif isinstance(tool_call, dict):
                    tool_calls.append(dict(tool_call))

        usage = ModelClient._field_value(completion, "usage")
        completion_details = ModelClient._field_value(
            usage, "completion_tokens_details"
        )
        reasoning_tokens = ModelClient._field_value(
            completion_details, "reasoning_tokens", 0
        )
        token_usage = TokenUsage(
            prompt_tokens=int(ModelClient._field_value(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(
                ModelClient._field_value(usage, "completion_tokens", 0) or 0
            ),
            total_tokens=int(ModelClient._field_value(usage, "total_tokens", 0) or 0),
            reasoning_tokens=int(reasoning_tokens or 0),
            cached_prompt_tokens=ModelClient._extract_cached_prompt_tokens(usage),
        )

        finish_reason = ""
        raw_finish_reason = ModelClient._field_value(choice, "finish_reason", "")
        if raw_finish_reason:
            finish_reason = str(raw_finish_reason)

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=token_usage,
            model=str(ModelClient._field_value(completion, "model", "") or ""),
            finish_reason=finish_reason,
            usage_present=usage is not None,
        )

    @staticmethod
    def _extract_cached_prompt_tokens(usage: Any) -> int | None:
        """Read common OpenAI-compatible cached-input usage shapes."""

        if usage is None:
            return None
        for name in (
            "cached_prompt_tokens",
            "cache_read_input_tokens",
            "cached_tokens",
        ):
            value = ModelClient._field_value(usage, name)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    continue
        for name in ("prompt_tokens_details", "input_tokens_details"):
            details = ModelClient._field_value(usage, name)
            value = ModelClient._field_value(details, "cached_tokens")
            if value is None:
                value = ModelClient._field_value(details, "cache_read_input_tokens")
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def _field_value(value: Any, name: str, default: Any = None) -> Any:
        """Read one field from either an SDK object or a dict response."""

        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _extract_thinking(completion: Any) -> str:
        choices = ModelClient._field_value(completion, "choices", []) or []
        choice = choices[0] if choices else None
        response_message = ModelClient._field_value(choice, "message")
        raw = ModelClient._field_value(response_message, "reasoning_content")
        return raw if isinstance(raw, str) else ""
