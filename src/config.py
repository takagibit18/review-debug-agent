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
    model_validator,
)

from src.analyzer.context_mode import ReviewContextMode

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env", override=True)

_base_url_adapter = TypeAdapter(AnyHttpUrl)
PermissionMode = Literal["default", "plan"]
TraceDetailMode = Literal["off", "compact", "full"]
ReviewWorkflowEnforcement = Literal["off", "warn", "enforce"]
ExecuteBackend = Literal["subprocess", "docker"]
GitHubAuthMode = Literal["token", "app"]
EvalGitSslBackend = Literal["system", "openssl", "schannel"]
RelationGraphResolverMode = Literal["ast", "resolver", "lsp"]

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


def _parse_optional_positive_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_allowed_commands(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return _DEFAULT_EXECUTE_ALLOWED_COMMANDS
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts or _DEFAULT_EXECUTE_ALLOWED_COMMANDS


def _default_execute_backend() -> ExecuteBackend:
    raw = (os.getenv("EXECUTE_BACKEND", "subprocess") or "subprocess").strip().lower()
    if raw in {"subprocess", "docker"}:
        return cast(ExecuteBackend, raw)
    return "subprocess"


def _default_github_auth_mode() -> GitHubAuthMode:
    raw = str(os.getenv("GITHUB_AUTH_MODE", "")).strip().lower()
    if raw in {"token", "app"}:
        return cast(GitHubAuthMode, raw)
    if _parse_bool_env("GITHUB_APP_MODE", False):
        return "app"
    return "token"


def _default_eval_git_ssl_backend() -> EvalGitSslBackend:
    raw = str(os.getenv("EVAL_GIT_SSL_BACKEND", "system")).strip().lower()
    if raw in {"system", "openssl", "schannel"}:
        return cast(EvalGitSslBackend, raw)
    return "system"


def _normalize_private_key_env(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1].strip()
    return value.replace("\\n", "\n")


def _default_agent_trace_detail() -> TraceDetailMode:
    raw = str(os.getenv("AGENT_TRACE_DETAIL", "off")).strip().lower() or "off"
    if raw in {"off", "compact", "full"}:
        return cast(TraceDetailMode, raw)
    return "off"


def _default_review_context_mode() -> ReviewContextMode:
    raw = str(os.getenv("REVIEW_CONTEXT_MODE", "")).strip().lower()
    if raw in {"agent_search", "graph_hybrid"}:
        return cast(ReviewContextMode, raw)
    return (
        "graph_hybrid"
        if _parse_bool_env("RELATION_GRAPH_ENABLED", True)
        else "agent_search"
    )


def _default_execute_docker_workdir() -> str:
    raw = str(os.getenv("EXECUTE_DOCKER_WORKDIR", "/workspace")).strip()
    if not raw or not raw.startswith("/"):
        return "/workspace"
    return raw.rstrip("/") or "/workspace"


def _default_execute_docker_network() -> str:
    raw = str(os.getenv("EXECUTE_DOCKER_NETWORK", "none")).strip()
    return raw or "none"


def _default_execute_docker_memory_mb() -> int:
    raw = str(os.getenv("EXECUTE_DOCKER_MEMORY_MB", "0")).strip() or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _default_execute_docker_cpus() -> float:
    raw = str(os.getenv("EXECUTE_DOCKER_CPUS", "0")).strip() or "0"
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


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
    model_provider: str = Field(
        default_factory=lambda: os.getenv("MODEL_PROVIDER", ""),
        description=(
            "Explicit OpenAI-compatible provider id; blank enables legacy profile detection."
        ),
    )
    log_level: str = Field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"),
    )
    review_max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("REVIEW_MAX_ITERATIONS", "16")),
        ge=1,
    )
    debug_max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("DEBUG_MAX_ITERATIONS", "3")),
        ge=1,
    )
    token_budget: int = Field(
        default_factory=lambda: int(os.getenv("TOKEN_BUDGET", "30000")),
        ge=1,
    )
    token_hard_budget: int = Field(
        default_factory=lambda: int(os.getenv("TOKEN_HARD_BUDGET", "36000")),
        ge=1,
    )
    final_submit_reserve_tokens: int = Field(
        default_factory=lambda: int(os.getenv("FINAL_SUBMIT_RESERVE_TOKENS", "12000")),
        ge=1,
        description="Tokens protected from normal analysis for one final structured submission.",
    )
    final_submit_prompt_token_budget: int = Field(
        default_factory=lambda: int(
            os.getenv("FINAL_SUBMIT_PROMPT_TOKEN_BUDGET", "4000")
        ),
        ge=1,
        description="Max truncatable context tokens in a finalize-only model request.",
    )
    final_submit_feedback_token_budget: int = Field(
        default_factory=lambda: int(
            os.getenv("FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET", "1200")
        ),
        ge=0,
        description=(
            "Portion of the finalize prompt budget reserved for accumulated tool "
            "evidence and prior-analysis concerns."
        ),
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
        default_factory=lambda: int(
            os.getenv("FILE_CONTEXT_MAX_CHARS_PER_FILE", "12000")
        ),
        ge=100,
        description="Max chars loaded per file for file_contents context.",
    )
    file_context_max_chars_total: int = Field(
        default_factory=lambda: int(
            os.getenv("FILE_CONTEXT_MAX_CHARS_TOTAL", "120000")
        ),
        ge=1000,
        description="Max aggregate chars loaded across file_contents context.",
    )
    context_summary_enabled: bool = Field(
        default_factory=lambda: (
            os.getenv("CONTEXT_SUMMARY_ENABLED", "true").strip().lower()
            in {"1", "true", "yes"}
        ),
        description="Enable second-layer LLM summarization for overflowed context parts",
    )
    summary_max_tokens_per_part: int = Field(
        default_factory=lambda: int(os.getenv("SUMMARY_MAX_TOKENS_PER_PART", "1000")),
        ge=100,
        description="Maximum completion tokens for one summarized context part",
    )
    model_max_tokens: int = Field(
        default_factory=lambda: int(os.getenv("MODEL_MAX_TOKENS", "2048")),
        ge=1,
        le=128000,
        description="Maximum completion tokens for a non-finalize model call.",
    )
    model_request_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "90")),
        gt=0.0,
        le=600.0,
        description="Hard wall-clock timeout for one model provider request.",
    )
    model_max_retries: int = Field(
        default_factory=lambda: int(os.getenv("MODEL_MAX_RETRIES", "1")),
        ge=1,
        le=3,
        description="Maximum provider attempts for one logical model call.",
    )
    root_cause_consolidation_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "ROOT_CAUSE_CONSOLIDATION_ENABLED", True
        ),
        description="Consolidate verified hypotheses into independent repair units.",
    )
    root_cause_consolidation_max_block_size: int = Field(
        default_factory=lambda: int(
            os.getenv("ROOT_CAUSE_CONSOLIDATION_MAX_BLOCK_SIZE", "16")
        ),
        ge=2,
        le=100,
    )
    root_cause_consolidation_conservative_mode: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "ROOT_CAUSE_CONSOLIDATION_CONSERVATIVE_MODE", True
        ),
        description="Require complete-link cluster compatibility and explicit yes counterfactuals.",
    )
    root_cause_consolidation_extra_retrieval_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "ROOT_CAUSE_CONSOLIDATION_EXTRA_RETRIEVAL_ENABLED", False
        ),
    )
    review_context_mode: ReviewContextMode = Field(
        default_factory=_default_review_context_mode,
        description="Explicit review context strategy. Constructor value overrides environment compatibility inputs.",
    )
    relation_graph_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env("RELATION_GRAPH_ENABLED", True),
        description="Compatibility alias derived from review_context_mode.",
    )
    relation_graph_persistence_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "RELATION_GRAPH_PERSISTENCE_ENABLED", True
        ),
    )
    relation_graph_index_path: str = Field(
        default_factory=lambda: os.getenv(
            "RELATION_GRAPH_INDEX_PATH", ".mergewarden/relation-index.sqlite3"
        ).strip(),
        min_length=1,
    )
    relation_graph_max_depth: int = Field(
        default_factory=lambda: int(os.getenv("RELATION_GRAPH_MAX_DEPTH", "2")),
        ge=0,
        le=6,
    )
    relation_graph_max_nodes: int = Field(
        default_factory=lambda: int(os.getenv("RELATION_GRAPH_MAX_NODES", "40")),
        ge=1,
        le=500,
    )
    relation_graph_max_context_tokens: int = Field(
        default_factory=lambda: int(
            os.getenv("RELATION_GRAPH_MAX_CONTEXT_TOKENS", "4000")
        ),
        ge=128,
        le=64000,
    )
    relation_graph_min_evidence_confidence: float = Field(
        default_factory=lambda: float(
            os.getenv("RELATION_GRAPH_MIN_EVIDENCE_CONFIDENCE", "0.65")
        ),
        ge=0.0,
        le=1.0,
    )
    relation_graph_lsp_enrichment_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "RELATION_GRAPH_LSP_ENRICHMENT_ENABLED", False
        ),
    )
    relation_graph_resolver_mode: RelationGraphResolverMode = Field(
        default_factory=lambda: cast(
            RelationGraphResolverMode,
            os.getenv("RELATION_GRAPH_RESOLVER_MODE", "ast").strip().lower(),
        ),
    )
    relation_graph_max_files: int = Field(
        default_factory=lambda: int(os.getenv("RELATION_GRAPH_MAX_FILES", "5000")),
        ge=1,
        le=100000,
    )
    relation_graph_max_ambiguous_targets: int = Field(
        default_factory=lambda: int(
            os.getenv("RELATION_GRAPH_MAX_AMBIGUOUS_TARGETS", "4")
        ),
        ge=1,
        le=100,
    )
    review_workflow_enforcement: ReviewWorkflowEnforcement = Field(
        default_factory=lambda: cast(
            ReviewWorkflowEnforcement,
            os.getenv("REVIEW_WORKFLOW_ENFORCEMENT", "enforce").strip().lower(),
        ),
    )
    agent_run_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "170")),
        gt=0.0,
        le=3600.0,
        description="Soft wall-clock deadline for one orchestrator run.",
    )
    agent_tool_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
        gt=0.0,
        le=600.0,
        description="Hard wall-clock timeout for one orchestrator tool call.",
    )
    agent_max_tool_calls: int = Field(
        default_factory=lambda: int(os.getenv("AGENT_MAX_TOOL_CALLS", "64")),
        ge=1,
        le=1000,
        description="Maximum successfully dispatched tool calls in one agent run.",
    )
    pre_budget_submit_token_ratio: float = Field(
        default_factory=lambda: float(
            os.getenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.80")
        ),
        ge=0.1,
        le=0.9,
        description="Ratio of token_budget at which a pre-budget submit-only call triggers when useful tool feedback exists.",
    )
    review_diff_first_changed_files: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "REVIEW_DIFF_FIRST_CHANGED_FILES", False
        ),
        description="Read changed diff files before the first review model call for A/B eval runs.",
    )
    review_diff_first_changed_files_max: int = Field(
        default_factory=lambda: int(
            os.getenv("REVIEW_DIFF_FIRST_CHANGED_FILES_MAX", "4")
        ),
        ge=1,
        le=20,
        description="Maximum number of changed diff files to pre-read before the first review model call.",
    )
    event_log_dir: str = Field(
        default_factory=lambda: os.getenv("EVENT_LOG_DIR", ".mergewarden/logs"),
        min_length=1,
    )
    agent_trace_detail: TraceDetailMode = Field(
        default_factory=_default_agent_trace_detail
    )
    agent_trace_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("AGENT_TRACE_MAX_CHARS", "1200")),
        ge=64,
    )
    agent_trace_log_tool_body: bool = Field(
        default_factory=lambda: (
            os.getenv("AGENT_TRACE_LOG_TOOL_BODY", "false").strip().lower()
            in {"1", "true", "yes"}
        ),
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
    eval_git_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("EVAL_GIT_TIMEOUT_SECONDS", "120")),
        gt=0.0,
        le=1800.0,
        description="Hard wall-clock timeout for eval fixture git operations.",
    )
    eval_git_ssl_backend: EvalGitSslBackend = Field(
        default_factory=_default_eval_git_ssl_backend,
        description="Per-command TLS backend for Eval Git operations.",
    )
    eval_workspace_cache_dir: str = Field(
        default_factory=lambda: (
            os.getenv(
                "EVAL_WORKSPACE_CACHE_DIR", "eval/outputs/workspace_cache"
            ).strip()
            or "eval/outputs/workspace_cache"
        ),
        min_length=1,
        description="Root directory for reusable Eval Git workspace mirrors.",
    )
    eval_offline_workspace_cache: bool = Field(
        default_factory=lambda: _parse_bool_env("EVAL_OFFLINE_WORKSPACE_CACHE", False),
        description="Use only existing local Git mirrors for eval fixture workspaces.",
    )
    eval_fixture_concurrency: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_FIXTURE_CONCURRENCY", "3")),
        ge=1,
    )
    eval_review_max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_REVIEW_MAX_ITERATIONS", "16")),
        ge=1,
    )
    eval_review_max_iterations_cap: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", "16")),
        ge=1,
    )
    eval_review_min_tool_iterations: int = Field(
        default_factory=lambda: int(os.getenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", "1")),
        ge=0,
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
    execute_docker_image: str = Field(
        default_factory=lambda: os.getenv(
            "EXECUTE_DOCKER_IMAGE", "mergewarden-execute:latest"
        ),
        min_length=1,
        description="Docker image used by the docker execute backend.",
    )
    execute_docker_workdir: str = Field(
        default_factory=_default_execute_docker_workdir,
        min_length=1,
        description="Container workdir where the workspace root is mounted.",
    )
    execute_docker_network: str = Field(
        default_factory=_default_execute_docker_network,
        min_length=1,
        description="Docker network mode for execute backend containers.",
    )
    execute_docker_memory_mb: int = Field(
        default_factory=_default_execute_docker_memory_mb,
        ge=0,
        description="Optional Docker memory limit in MB; 0 disables the limit.",
    )
    execute_docker_cpus: float = Field(
        default_factory=_default_execute_docker_cpus,
        ge=0.0,
        description="Optional Docker CPU quota; 0 disables the limit.",
    )
    github_advisory_dry_run: bool = Field(
        default_factory=lambda: _parse_bool_env("GITHUB_ADVISORY_DRY_RUN", True),
        description="Default GitHub advisory publish mode; true keeps CLI publishing in dry-run mode.",
    )
    github_advisory_comment_marker: str = Field(
        default_factory=lambda: os.getenv(
            "GITHUB_ADVISORY_COMMENT_MARKER",
            "<!-- mergewarden:comment -->",
        ),
        min_length=1,
        description="Hidden marker used to identify MergeWarden-owned GitHub review comments.",
    )
    github_auth_mode: GitHubAuthMode = Field(
        default_factory=_default_github_auth_mode,
        description="GitHub API auth mode: token uses GITHUB_TOKEN/GH_TOKEN, app uses installation tokens.",
    )
    github_app_id: str = Field(
        default_factory=lambda: os.getenv("GITHUB_APP_ID", "").strip(),
        description="GitHub App numeric app id.",
    )
    github_private_key: str = Field(
        default_factory=lambda: _normalize_private_key_env(
            os.getenv("GITHUB_PRIVATE_KEY", "")
        ),
        description="GitHub App PEM private key; literal \\n sequences are supported.",
    )
    github_webhook_secret: str = Field(
        default_factory=lambda: os.getenv("GITHUB_WEBHOOK_SECRET", "").strip(),
        description="GitHub App webhook secret used for X-Hub-Signature-256 verification.",
    )
    github_app_client_id: str = Field(
        default_factory=lambda: os.getenv("GITHUB_APP_CLIENT_ID", "").strip(),
        description="Optional GitHub App client id for future OAuth flows.",
    )
    github_app_client_secret: str = Field(
        default_factory=lambda: os.getenv("GITHUB_APP_CLIENT_SECRET", "").strip(),
        description="Optional GitHub App client secret for future OAuth flows.",
    )
    app_base_url: str = Field(
        default_factory=lambda: (
            os.getenv("APP_BASE_URL")
            or os.getenv("PUBLIC_URL")
            or "http://localhost:8000"
        ).strip(),
        description="Public base URL used when configuring GitHub App webhooks.",
    )
    github_review_draft_prs: bool = Field(
        default_factory=lambda: _parse_bool_env("GITHUB_REVIEW_DRAFT_PRS", False),
        description="Whether GitHub App webhook reviews draft pull requests.",
    )
    github_webhook_allow_rerun: bool = Field(
        default_factory=lambda: _parse_bool_env("GITHUB_WEBHOOK_ALLOW_RERUN", False),
        description="Allow duplicate delivery/repo/pull/head review execution from webhooks.",
    )
    platform_database_url: str = Field(
        default_factory=lambda: os.getenv(
            "PLATFORM_DATABASE_URL",
            "sqlite:///.mergewarden/platform.db",
        ).strip(),
        min_length=1,
        description="SQLite database URL for the platform MVP.",
    )
    platform_artifact_root: str = Field(
        default_factory=lambda: os.getenv(
            "PLATFORM_ARTIFACT_ROOT",
            ".mergewarden/platform-artifacts",
        ).strip(),
        min_length=1,
        description="Root directory for platform run artifacts.",
    )
    platform_init_db_on_startup: bool = Field(
        default_factory=lambda: _parse_bool_env("PLATFORM_INIT_DB_ON_STARTUP", True),
        description="Initialize platform tables on API/worker startup.",
    )
    platform_public_github_app_only: bool = Field(
        default_factory=lambda: _parse_bool_env(
            "PLATFORM_PUBLIC_GITHUB_APP_ONLY",
            False,
        ),
        description=(
            "Expose only health and GitHub webhook routes for the hosted public API."
        ),
    )
    platform_review_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env("PLATFORM_REVIEW_ENABLED", True),
        description="Global default for whether webhook reviews are enabled.",
    )
    platform_publish_comments: bool = Field(
        default_factory=lambda: _parse_bool_env("PLATFORM_PUBLISH_COMMENTS", True),
        description="Global default for publishing GitHub inline comments.",
    )
    platform_default_tenant_id: int | None = Field(
        default_factory=lambda: _parse_optional_positive_int_env(
            "PLATFORM_DEFAULT_TENANT_ID",
        ),
        description=(
            "Optional development tenant id used when platform management requests "
            "omit the tenant header."
        ),
    )
    platform_worker_poll_interval_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("PLATFORM_WORKER_POLL_INTERVAL_SECONDS", "2.0")
        ),
        gt=0.0,
        le=3600.0,
        description="Polling interval for the local DB-backed platform worker.",
    )
    platform_worker_single_worker: bool = Field(
        default_factory=lambda: _parse_bool_env("PLATFORM_WORKER_SINGLE_WORKER", True),
        description="Documented MVP guard: SQLite worker is intended for a single local worker.",
    )
    run_checkpoints_enabled: bool = Field(
        default_factory=lambda: _parse_bool_env("RUN_CHECKPOINTS_ENABLED", True),
        description="Persist worker pipeline checkpoints for crash recovery.",
    )
    run_lease_seconds: int = Field(
        default_factory=lambda: int(os.getenv("RUN_LEASE_SECONDS", "180")),
        ge=30,
        le=3600,
    )
    run_heartbeat_seconds: int = Field(
        default_factory=lambda: int(os.getenv("RUN_HEARTBEAT_SECONDS", "30")),
        ge=1,
        le=300,
    )

    @field_validator("openai_api_key", "model_name", mode="before")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @model_validator(mode="after")
    def _normalize_budget_relationships(self) -> "Settings":
        self.token_hard_budget = max(self.token_hard_budget, self.token_budget)
        self.final_submit_reserve_tokens = min(
            self.final_submit_reserve_tokens,
            self.token_hard_budget,
        )
        self.final_submit_prompt_token_budget = min(
            self.final_submit_prompt_token_budget,
            self.prompt_input_token_budget,
        )
        self.final_submit_feedback_token_budget = min(
            self.final_submit_feedback_token_budget,
            max(0, self.final_submit_prompt_token_budget - 1),
        )
        if (
            "review_context_mode" not in self.model_fields_set
            and "relation_graph_enabled" in self.model_fields_set
        ):
            self.review_context_mode = (
                "graph_hybrid" if self.relation_graph_enabled else "agent_search"
            )
        self.relation_graph_enabled = self.review_context_mode == "graph_hybrid"
        if self.relation_graph_lsp_enrichment_enabled:
            self.relation_graph_resolver_mode = "lsp"
        return self

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
            return ".mergewarden/logs"
        raw = str(value).strip()
        return raw or ".mergewarden/logs"

    @field_validator("relation_graph_index_path", mode="before")
    @classmethod
    def _validate_relation_graph_index_path(cls, value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("RELATION_GRAPH_INDEX_PATH must not be empty")
        return raw

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

    @field_validator("execute_docker_image", mode="before")
    @classmethod
    def _validate_execute_docker_image(cls, value: object) -> str:
        if value is None:
            return "mergewarden-execute:latest"
        raw = str(value).strip()
        return raw or "mergewarden-execute:latest"

    @field_validator("execute_docker_workdir", mode="before")
    @classmethod
    def _validate_execute_docker_workdir(cls, value: object) -> str:
        if value is None:
            return "/workspace"
        raw = str(value).strip()
        if not raw or not raw.startswith("/"):
            return "/workspace"
        return raw.rstrip("/") or "/workspace"

    @field_validator("execute_docker_network", mode="before")
    @classmethod
    def _validate_execute_docker_network(cls, value: object) -> str:
        if value is None:
            return "none"
        raw = str(value).strip()
        return raw or "none"

    @field_validator("github_advisory_comment_marker", mode="before")
    @classmethod
    def _validate_github_advisory_comment_marker(cls, value: object) -> str:
        if value is None:
            return "<!-- mergewarden:comment -->"
        raw = str(value).strip()
        return raw or "<!-- mergewarden:comment -->"

    @field_validator("github_auth_mode", mode="before")
    @classmethod
    def _validate_github_auth_mode(cls, value: object) -> str:
        if value is None:
            return _default_github_auth_mode()
        raw = str(value).strip().lower()
        if raw in {"token", "app"}:
            return raw
        return _default_github_auth_mode()

    @field_validator("github_private_key", mode="before")
    @classmethod
    def _validate_github_private_key(cls, value: object) -> str:
        if value is None:
            return ""
        return _normalize_private_key_env(str(value))

    @field_validator("platform_database_url", mode="before")
    @classmethod
    def _validate_platform_database_url(cls, value: object) -> str:
        if value is None:
            return "sqlite:///.mergewarden/platform.db"
        raw = str(value).strip()
        return raw or "sqlite:///.mergewarden/platform.db"

    @field_validator("platform_artifact_root", mode="before")
    @classmethod
    def _validate_platform_artifact_root(cls, value: object) -> str:
        if value is None:
            return ".mergewarden/platform-artifacts"
        raw = str(value).strip()
        return raw or ".mergewarden/platform-artifacts"

    @field_validator("execute_docker_memory_mb", mode="before")
    @classmethod
    def _validate_execute_docker_memory_mb(cls, value: object) -> int:
        if value is None:
            return 0
        try:
            return max(0, int(str(value).strip() or "0"))
        except ValueError:
            return 0

    @field_validator("execute_docker_cpus", mode="before")
    @classmethod
    def _validate_execute_docker_cpus(cls, value: object) -> float:
        if value is None:
            return 0.0
        try:
            return max(0.0, float(str(value).strip() or "0"))
        except ValueError:
            return 0.0


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
