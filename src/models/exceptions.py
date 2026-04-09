"""Model-layer exceptions with structured metadata."""

from __future__ import annotations


class ModelClientError(Exception):
    """Base error for all model client failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "openai-compatible",
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code


class AuthenticationError(ModelClientError):
    """API key missing or rejected by the provider."""


class RateLimitError(ModelClientError):
    """The provider rejected the request due to rate limits."""


class ModelTimeoutError(ModelClientError):
    """The request exceeded the configured timeout."""


class ServiceUnavailableError(ModelClientError):
    """The provider service is temporarily unavailable."""
