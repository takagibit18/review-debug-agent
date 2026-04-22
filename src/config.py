"""Global configuration management.

Loads settings from environment variables (with .env support) and exposes
them as a validated Pydantic model for use across all modules.
"""

import os
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env", override=True)

_base_url_adapter = TypeAdapter(AnyHttpUrl)
PermissionMode = Literal["default", "plan"]
TraceDetailMode = Literal["off", "compact", "full"]
ExecuteBackend = Literal["subprocess", "docker"]

_DEFAULT_EXECUTE_ALLOWED_COMMANDS: tuple[str, ...] = (
    "python",
    "pytest",
    "pip",
    "node",
    "npm",
    "ruff",
    "mypy",
    "git",
)


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def _parse_allowed_commands(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return _DEFAULT_EXECUTE_ALLOWED_COMMANDS
    parts = tuple(
        item.strip() for item in raw.split(",") if item.strip()
    )
    return parts or _DEFAULT_EXECUTE_ALLOWED_COMMANDS


def _default_execute_backend() -> ExecuteBackend:
    raw = (os.getenv("EXECUTE_BACKEND", "subprocess") or "subprocess").strip().lower()
    if raw in {"subprocess", "docker"}:
        return cast(ExecuteBackend, raw)
    return "subprocess"


def _default_agent_trace_detail() -> TraceDetailMode:
    raw = str(os.getenv("AGENT_TRACE_DETAIL", "off")).strip().lower() or "off"
    if raw in {"off", "compact", "full"}:
        return cast(TraceDetailMode, raw)
    return "off"


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
        default_factory=lambda: int(os.getenv("TOKEN_BUDGET", "24000")),
        ge=1,
    )
    feedback_window_iterations: int = Field(
        default_factory=lambda: int(os.getenv("FEEDBACK_WINDOW_ITERATIONS", "3")),
        ge=1,
        description="How many recent iterations of tool feedback are injected verbatim into the prompt.",
    )
    prompt_input_token_budget: int = Field(
        default_factory=lambda: int(os.getenv("PROMPT_INPUT_TOKEN_BUDGET", "32000")),
        ge=1,
        description="Max estimated tokens for truncatable context parts (meta, diff, files, structure)",
    )
    project_structure_max_depth: int = Field(
        default_factory=lambda: int(os.getenv("PROJECT_STRUCTURE_MAX_DEPTH", "3")),
        ge=1,
        le=8,
        description="Max tree depth included in project_structure context.",
    )
    project_structure_max_entries: int = Field(
        default_factory=lambda: int(os.getenv("PROJECT_STRUCTURE_MAX_ENTRIES", "200")),
        ge=10,
        description="Max number of file/dir entries included in project_structure context.",
    )
    file_context_max_files: int = Field(
        default_factory=lambda: int(os.getenv("FILE_CONTEXT_MAX_FILES", "20")),
        ge=1,
        description="Max number of files loaded into file_contents context.",
    )
    file_context_max_chars_per_file: int = Field(
        default_factory=lambda: int(os.getenv("FILE_CONTEXT_MAX_CHARS_PER_FILE", "12000")),
        ge=100,
        description="Max chars loaded per file for file_contents context.",
    )
    file_context_max_chars_total: int = Field(
        default_factory=lambda: int(os.getenv("FILE_CONTEXT_MAX_CHARS_TOTAL", "120000")),
        ge=1000,
        description="Max aggregate chars loaded across file_contents context.",
    )
    context_summary_enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "CONTEXT_SUMMARY_ENABLED", "true"
        ).strip().lower()
        in {"1", "true", "yes"},
        description="Enable second-layer LLM summarization for overflowed context parts",
    )
    summary_max_tokens_per_part: int = Field(
        default_factory=lambda: int(os.getenv("SUMMARY_MAX_TOKENS_PER_PART", "1000")),
        ge=100,
        description="Maximum completion tokens for one summarized context part",
    )
    event_log_dir: str = Field(
        default_factory=lambda: os.getenv("EVENT_LOG_DIR", ".cr-debug-agent/logs"),
        min_length=1,
    )
    agent_trace_detail: TraceDetailMode = Field(default_factory=_default_agent_trace_detail)
    agent_trace_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("AGENT_TRACE_MAX_CHARS", "1200")),
        ge=64,
    )
    agent_trace_log_tool_body: bool = Field(
        default_factory=lambda: os.getenv("AGENT_TRACE_LOG_TOOL_BODY", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
    )
    eval_temperature: float = Field(
        default_factory=lambda: float(os.getenv("EVAL_TEMPERATURE", "0.0")),
        ge=0.0,
        le=2.0,
    )
    eval_samples: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_SAMPLES", "1")),
        ge=1,
    )
    eval_concurrency: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_CONCURRENCY", "1")),
        ge=1,
    )
    permission_mode: PermissionMode = Field(
        default="default",
    )
    execute_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env("EXECUTE_ENABLED", True),
        description="Global switch for execute-class tools; disables registration even in debug mode when False.",
    )
    execute_backend: ExecuteBackend = Field(
        default_factory=_default_execute_backend,
        description="Backend used for running execute-class commands (subprocess | docker).",
    )
    execute_allowed_commands: tuple[str, ...] = Field(
        default_factory=lambda: _parse_allowed_commands(
            os.getenv("EXECUTE_ALLOWED_COMMANDS")
        ),
        description="Allowed first-token commands for run_command; enforced by exec_policy.",
    )
    execute_default_timeout_ms: int = Field(
        default_factory=lambda: int(os.getenv("EXECUTE_DEFAULT_TIMEOUT_MS", "30000")),
        ge=1,
        le=600_000,
    )
    execute_max_output_bytes: int = Field(
        default_factory=lambda: int(os.getenv("EXECUTE_MAX_OUTPUT_BYTES", "65536")),
        ge=1024,
        description="Per-stream (stdout/stderr) byte cap; exceeded output is truncated with a marker.",
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

    @field_validator("execute_backend", mode="before")
    @classmethod
    def _validate_execute_backend(cls, value: object) -> str:
        if value is None:
            return "subprocess"
        raw = str(value).strip().lower()
        if raw in {"subprocess", "docker"}:
            return raw
        return "subprocess"

    @field_validator("execute_allowed_commands", mode="before")
    @classmethod
    def _validate_execute_allowed_commands(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return _DEFAULT_EXECUTE_ALLOWED_COMMANDS
        if isinstance(value, str):
            return _parse_allowed_commands(value)
        if isinstance(value, (list, tuple)):
            parts = tuple(str(v).strip() for v in value if str(v).strip())
            return parts or _DEFAULT_EXECUTE_ALLOWED_COMMANDS
        return _DEFAULT_EXECUTE_ALLOWED_COMMANDS

    @field_validator("agent_trace_detail", mode="before")
    @classmethod
    def _validate_agent_trace_detail(cls, value: object) -> str:
        if value is None:
            return "off"
        raw = str(value).strip().lower()
        if raw in {"off", "compact", "full"}:
            return raw
        return "off"


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
