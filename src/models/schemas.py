"""Typed model-layer schemas used across analyzer and orchestrator."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """Validated runtime configuration for a single model call."""

    model: str = Field(..., min_length=1, description="Model name to call")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: int = Field(
        default=2048, ge=1, le=128000, description="Maximum response tokens"
    )
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="Nucleus sampling")
    timeout: float = Field(
        default=90.0, gt=0.0, le=600.0, description="Request timeout in seconds"
    )
    tool_choice: str | dict[str, Any] | None = Field(
        default=None,
        description="Optional provider tool-choice control for chat completions",
    )
    extra_body: dict[str, Any] | None = Field(
        default=None,
        description="Optional provider-specific request body extension",
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
    thinking: str | None = Field(
        default=None,
        description="Canonical transient thinking retained only by the model layer",
    )


class TokenUsage(BaseModel):
    """Token accounting returned by the provider."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(
        default=0,
        ge=0,
        description="Provider-reported reasoning token count; never reasoning content",
    )


class ModelResponse(BaseModel):
    """Structured model output used by the rest of the system."""

    content: str = Field(default="", description="Assistant message text")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Structured tool calls"
    )
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = Field(default="", description="Provider model id in response")
    finish_reason: str = Field(default="", description="Provider finish reason")


class DraftFindingInput(BaseModel):
    """Weak model-controlled hypothesis schema for review work in progress."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file: str = Field(..., min_length=1, description="Repository-relative suspect file")
    claim: str = Field(..., min_length=1, description="Minimal suspected behavior")
    line: int | None = Field(
        default=None,
        ge=1,
        description="Optional suspect line",
    )
    symbol: str | None = Field(
        default=None,
        min_length=1,
        description="Optional suspect symbol",
    )


class DraftFinding(BaseModel):
    """Runtime-bound durable review hypothesis."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(..., min_length=1, description="Runtime-generated draft id")
    source_response_id: str = Field(
        ...,
        min_length=1,
        description="Journal id of the originating model response",
    )
    file: str = Field(..., min_length=1, description="Repository-relative suspect file")
    line: int | None = Field(default=None, ge=1, description="Optional suspect line")
    symbol: str | None = Field(
        default=None,
        min_length=1,
        description="Optional suspect symbol",
    )
    claim: str = Field(..., min_length=1, description="Minimal suspected behavior")
