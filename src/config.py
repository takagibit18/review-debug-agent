"""Global configuration management.

Loads settings from environment variables (with .env support) and exposes
them as a validated Pydantic model for use across all modules.
"""

import os

from dotenv import load_dotenv
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

load_dotenv()


class Settings(BaseModel):
    """Application-wide settings loaded from environment."""

    model_config = ConfigDict(validate_default=True)

    openai_api_key: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", ""),
        min_length=1,
    )
    openai_base_url: AnyHttpUrl = Field(
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

    @field_validator("openai_api_key", "model_name", mode="before")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()


def get_settings() -> Settings:
    """Return a Settings instance populated from environment."""
    return Settings()
