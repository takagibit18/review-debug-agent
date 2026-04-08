"""Global configuration management.

Loads settings from environment variables (with .env support) and exposes
them as a validated Pydantic model for use across all modules.
"""

from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os

load_dotenv()


class Settings(BaseModel):
    """Application-wide settings loaded from environment."""

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
    )
    log_level: str = Field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"),
    )


def get_settings() -> Settings:
    """Return a Settings instance populated from environment."""
    return Settings()
