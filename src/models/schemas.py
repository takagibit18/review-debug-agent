"""Typed model-layer schemas used across analyzer and orchestrator."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Validated runtime configuration for a single model call."""

    model: str = Field(..., min_length=1, description="Model name to call")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: int = Field(
        default=4096, ge=1, le=128000, description="Maximum response tokens"
    )
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="Nucleus sampling")
    timeout: float = Field(
        default=60.0, gt=0.0, le=600.0, description="Request timeout in seconds"
    )


class Message(BaseModel):
    """A normalized chat message used by the model client."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(default="", description="Natural language content")
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="Tool-call payload for assistant messages"
    )
    tool_call_id: str | None = Field(
        default=None, description="Tool call id for tool role messages"
    )


class TokenUsage(BaseModel):
    """Token accounting returned by the provider."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ModelResponse(BaseModel):
    """Structured model output used by the rest of the system."""

    content: str = Field(default="", description="Assistant message text")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Structured tool calls"
    )
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = Field(default="", description="Provider model id in response")
    finish_reason: str = Field(default="", description="Provider finish reason")
