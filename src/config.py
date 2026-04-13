"""Global configuration management.

Loads settings from environment variables (with .env support) and exposes
them as a validated Pydantic model for use across all modules.
"""

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

load_dotenv()

_base_url_adapter = TypeAdapter(AnyHttpUrl)
PermissionMode = Literal["default", "plan"]


class Settings(BaseModel):
    """Application-wide settings loaded from environment."""

    model_config = ConfigDict(validate_default=True)

    openai_api_key: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", ""),
    )
    openai_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        ),
    )
    model_name: str = Field(
        default_factory=lambda: os.getenv("MODEL_NAME", "gpt-4o"),
        min_length=1,
    )
    log_level: str = Field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"),
    )
    review_max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("REVIEW_MAX_ITERATIONS", "1")),
        ge=1,
    )
    debug_max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("DEBUG_MAX_ITERATIONS", "3")),
        ge=1,
    )
    token_budget: int = Field(
        default_factory=lambda: int(os.getenv("TOKEN_BUDGET", "12000")),
        ge=1,
    )
    prompt_input_token_budget: int = Field(
        default_factory=lambda: int(os.getenv("PROMPT_INPUT_TOKEN_BUDGET", "32000")),
        ge=1,
        description="Max estimated tokens for truncatable context parts (meta, diff, files, structure)",
    )
    event_log_dir: str = Field(
        default_factory=lambda: os.getenv("EVENT_LOG_DIR", ".cr-debug-agent/logs"),
        min_length=1,
    )
    permission_mode: PermissionMode = Field(
        default="default",
    )

    @field_validator("openai_api_key", "model_name", mode="before")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def _validate_openai_base_url(cls, value: object) -> str:
        if value is None:
            raw = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        else:
            raw = str(value).strip()
            if not raw:
                raw = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        _base_url_adapter.validate_python(raw)
        return raw

    @field_validator("event_log_dir", mode="before")
    @classmethod
    def _validate_event_log_dir(cls, value: object) -> str:
        if value is None:
            return ".cr-debug-agent/logs"
        raw = str(value).strip()
        return raw or ".cr-debug-agent/logs"


def _resolve_permission_mode(raw: object) -> PermissionMode:
    value = str(raw).strip().lower()
    if value == "plan":
        return "plan"
    return "default"


def get_settings() -> Settings:
    """Return a Settings instance populated from environment."""
    return Settings(
        permission_mode=_resolve_permission_mode(
            os.getenv("PERMISSION_MODE", "default")
        )
    )
