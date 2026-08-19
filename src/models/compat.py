"""Minimal provider compatibility metadata for OpenAI-compatible models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings

ThinkingFormat = Literal["none", "deepseek", "dashscope"]
ThinkingPolicy = Literal["off", "high"]


class ProviderCompat(BaseModel):
    """Wire-level capabilities needed by the current model call path."""

    model_config = ConfigDict(frozen=True)

    thinking_format: ThinkingFormat = "none"
    supports_reasoning_effort: bool = False
    supports_tool_choice_with_thinking: bool = True
    requires_reasoning_replay_for_tool_calls: bool = False
    requires_assistant_content_for_tool_calls: bool = False
    # Some dated model snapshots (e.g. qwen3.7-max-2026-05-17) reject ANY request
    # whose wire-level thinking switch is off ("enable_thinking" must be True).
    # When set, the client forces the thinking switch ON at the wire layer for
    # every call, regardless of the per-call reasoning policy. Default off so
    # behaviour for all other models is unchanged.
    force_enable_thinking: bool = False


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


def resolve_model_profile(settings: Settings, model: str) -> ModelProfile:
    """Resolve explicit provider metadata with one legacy configuration fallback."""

    provider = str(getattr(settings, "model_provider", "") or "").strip().lower()
    if not provider:
        provider = _legacy_provider(settings, model)
    if provider == "deepseek":
        compat = _DEEPSEEK_COMPAT
    elif provider == "dashscope":
        compat = _DASHSCOPE_COMPAT
    else:
        provider = provider or "openai"
        compat = ProviderCompat()
    # Dated qwen3.7-max snapshots mandate enable_thinking=True on every request;
    # the framework default submit policy sends it off and is rejected (HTTP 400).
    # Scope the override to the exact dated snapshot family so other models
    # (including the undated qwen3.7-max) keep their current behaviour.
    normalized_model = model.strip().lower()
    if normalized_model.startswith("qwen3.7-max-2026-"):
        compat = compat.model_copy(update={"force_enable_thinking": True})
    return ModelProfile(provider=provider, model=model, compat=compat)


def _legacy_provider(settings: Settings, model: str) -> str:
    """Infer old configurations only at the profile-resolution boundary."""

    normalized_model = model.strip().lower()
    base_url = str(getattr(settings, "openai_base_url", "") or "").strip().lower()
    if normalized_model.startswith("deepseek") or "deepseek" in base_url:
        return "deepseek"
    if normalized_model.startswith(("qwen", "glm")) or "dashscope" in base_url:
        return "dashscope"
    return "openai"
