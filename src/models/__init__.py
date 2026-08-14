"""Model layer — LLM provider abstraction.

Wraps the OpenAI-compatible API (and potentially other providers) behind a
unified async interface so the rest of the codebase is provider-agnostic.
"""

from src.models.client import ModelClient
from src.models.compat import ModelCallPolicy, ModelProfile, ProviderCompat
from src.models.conversation import AssistantToolTurn, ModelConversation, ToolResultTurn
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
    "AssistantToolTurn",
    "Message",
    "ModelClient",
    "ModelClientError",
    "ModelCallPolicy",
    "ModelConfig",
    "ModelConversation",
    "ModelResponse",
    "ModelProfile",
    "ModelTimeoutError",
    "RateLimitError",
    "ProviderCompat",
    "ServiceUnavailableError",
    "TokenUsage",
    "ToolResultTurn",
]
