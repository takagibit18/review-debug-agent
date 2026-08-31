"""Minimal provider compatibility metadata for OpenAI-compatible models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings

ThinkingFormat = Literal["none", "deepseek", "dashscope", "zhipu"]
ThinkingPolicy = Literal["off", "high"]


class ProviderCompat(BaseModel):
    """Wire-level capabilities needed by the current model call path."""

    model_config = ConfigDict(frozen=True)

    thinking_format: ThinkingFormat = "none"
    supports_reasoning_effort: bool = False
    supports_thinking_disable: bool = True
    supports_tool_choice_with_thinking: bool = True
    requires_reasoning_replay_for_tool_calls: bool = False
    requires_assistant_content_for_tool_calls: bool = False


class ModelProfile(BaseModel):
    """Resolved provider, API family, model name, and compatibility metadata."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., min_length=1)
    api: Literal["openai-completions"] = "openai-completions"
    model: str = Field(..., min_length=1)
    compat: ProviderCompat = Field(default_factory=ProviderCompat)


class ModelCallPolicy(BaseModel):
    """Provider-neutral reasoning and forced-tool intent for one model call."""

    model_config = ConfigDict(frozen=True)

    thinking: ThinkingPolicy = "off"
    forced_tool: str | None = None


_DEEPSEEK_COMPAT = ProviderCompat(
    thinking_format="deepseek",
    supports_reasoning_effort=True,
    supports_tool_choice_with_thinking=False,
    requires_reasoning_replay_for_tool_calls=True,
    requires_assistant_content_for_tool_calls=True,
)
_DASHSCOPE_COMPAT = ProviderCompat(
    thinking_format="dashscope",
    supports_tool_choice_with_thinking=False,
)


def _zhipu_compat(model: str) -> ProviderCompat:
    """Resolve the small set of GLM capabilities used by the agent loop."""

    normalized_model = model.strip().lower()
    glm_53 = normalized_model.startswith("glm-5.3")
    return ProviderCompat(
        thinking_format="zhipu",
        supports_reasoning_effort=normalized_model.startswith(("glm-5.2", "glm-5.3")),
        supports_thinking_disable=not glm_53,
        requires_reasoning_replay_for_tool_calls=True,
        requires_assistant_content_for_tool_calls=True,
    )


def resolve_model_profile(settings: Settings, model: str) -> ModelProfile:
    """Resolve explicit provider metadata with one legacy configuration fallback."""

    provider = str(getattr(settings, "model_provider", "") or "").strip().lower()
    if not provider:
        provider = _legacy_provider(settings, model)
    if provider == "deepseek":
        compat = _DEEPSEEK_COMPAT
    elif provider == "dashscope":
        compat = _DASHSCOPE_COMPAT
    elif provider in {"zhipu", "bigmodel", "zhipuai"}:
        provider = "zhipu"
        compat = _zhipu_compat(model)
    else:
        provider = provider or "openai"
        compat = ProviderCompat()
    return ModelProfile(provider=provider, model=model, compat=compat)


def _legacy_provider(settings: Settings, model: str) -> str:
    """Infer old configurations only at the profile-resolution boundary."""

    normalized_model = model.strip().lower()
    base_url = str(getattr(settings, "openai_base_url", "") or "").strip().lower()
    if normalized_model.startswith("deepseek") or "deepseek" in base_url:
        return "deepseek"
    if "bigmodel.cn" in base_url or "zhipu" in base_url:
        return "zhipu"
    if normalized_model.startswith(("qwen", "glm")) or "dashscope" in base_url:
        return "dashscope"
    return "openai"
