"""Model layer — LLM provider abstraction.

Wraps the OpenAI-compatible API (and potentially other providers) behind a
unified async interface so the rest of the codebase is provider-agnostic.
"""

from src.models.client import ModelClient
from src.models.exceptions import (
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.models.schemas import Message, ModelConfig, ModelResponse, TokenUsage

__all__ = [
    "AuthenticationError",
    "Message",
    "ModelClient",
    "ModelClientError",
    "ModelConfig",
    "ModelResponse",
    "ModelTimeoutError",
    "RateLimitError",
    "ServiceUnavailableError",
    "TokenUsage",
]
