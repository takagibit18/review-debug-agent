"""Tests for configuration normalization and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import get_settings


def test_public_github_app_only_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_PUBLIC_GITHUB_APP_ONLY", raising=False)

    assert get_settings().platform_public_github_app_only is False


def test_review_skill_retrieval_defaults_and_environment(monkeypatch) -> None:
    for name in (
        "REVIEW_SKILL_RETRIEVAL_MODE",
        "REVIEW_SKILL_TOP_K",
        "REVIEW_SKILL_CHAR_BUDGET",
        "REVIEW_SKILL_LEGACY_FALLBACK_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = get_settings()
    assert defaults.review_skill_retrieval_mode == "sequential"
    assert defaults.review_skill_top_k == 5
    assert defaults.review_skill_char_budget == 4000
    assert defaults.review_skill_legacy_fallback_limit == 1

    monkeypatch.setenv("REVIEW_SKILL_RETRIEVAL_MODE", "deterministic")
    monkeypatch.setenv("REVIEW_SKILL_TOP_K", "3")
    assert get_settings().review_skill_retrieval_mode == "deterministic"
    assert get_settings().review_skill_top_k == 3


def test_review_skill_retrieval_rejects_invalid_mode(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_SKILL_RETRIEVAL_MODE", "random")
    with pytest.raises(ValidationError):
        get_settings()


def test_public_github_app_only_reads_true_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("PLATFORM_PUBLIC_GITHUB_APP_ONLY", "true")

    assert get_settings().platform_public_github_app_only is True


def test_permission_mode_falls_back_to_default_for_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("PERMISSION_MODE", "invalid-mode")

    settings = get_settings()

    assert settings.permission_mode == "default"


def test_docker_execute_settings_have_expected_defaults(monkeypatch) -> None:
    monkeypatch.delenv("EXECUTE_DOCKER_IMAGE", raising=False)
    monkeypatch.delenv("EXECUTE_DOCKER_WORKDIR", raising=False)
    monkeypatch.delenv("EXECUTE_DOCKER_NETWORK", raising=False)
    monkeypatch.delenv("EXECUTE_DOCKER_MEMORY_MB", raising=False)
    monkeypatch.delenv("EXECUTE_DOCKER_CPUS", raising=False)

    settings = get_settings()

    assert settings.execute_docker_image == "mergewarden-execute:latest"
    assert settings.execute_docker_workdir == "/workspace"
    assert settings.execute_docker_network == "none"
    assert settings.execute_docker_memory_mb == 0
    assert settings.execute_docker_cpus == 0.0


def test_docker_execute_settings_normalize_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTE_DOCKER_IMAGE", " custom:image ")
    monkeypatch.setenv("EXECUTE_DOCKER_WORKDIR", " workspace ")
    monkeypatch.setenv("EXECUTE_DOCKER_NETWORK", "")
    monkeypatch.setenv("EXECUTE_DOCKER_MEMORY_MB", "-10")
    monkeypatch.setenv("EXECUTE_DOCKER_CPUS", "-1")

    settings = get_settings()

    assert settings.execute_docker_image == "custom:image"
    assert settings.execute_docker_workdir == "/workspace"
    assert settings.execute_docker_network == "none"
    assert settings.execute_docker_memory_mb == 0
    assert settings.execute_docker_cpus == 0.0


def test_workspace_eval_budget_defaults_are_bounded(monkeypatch) -> None:
    monkeypatch.delenv("TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("TOKEN_HARD_BUDGET", raising=False)
    monkeypatch.delenv("FINAL_SUBMIT_RESERVE_TOKENS", raising=False)
    monkeypatch.delenv("FINAL_SUBMIT_PROMPT_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_MAX_RETRIES", raising=False)
    monkeypatch.delenv("AGENT_RUN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGENT_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("EVAL_GIT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("EVAL_GIT_SSL_BACKEND", raising=False)

    settings = get_settings()

    assert settings.token_budget == 30_000
    assert settings.token_hard_budget == 36_000
    assert settings.final_submit_reserve_tokens == 12_000
    assert settings.final_submit_prompt_token_budget == 4_000
    assert settings.final_submit_feedback_token_budget == 1_200
    assert settings.model_max_tokens == 2_048
    assert settings.model_request_timeout_seconds == 90.0
    assert settings.model_max_retries == 1
    assert settings.agent_run_timeout_seconds == 170.0
    assert settings.agent_tool_timeout_seconds == 30.0
    assert settings.eval_git_timeout_seconds == 120.0
    assert settings.eval_git_ssl_backend == "system"


def test_eval_git_ssl_backend_is_normalized(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_GIT_SSL_BACKEND", " OPENSSL ")

    assert get_settings().eval_git_ssl_backend == "openssl"


def test_eval_workspace_cache_root_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("EVAL_WORKSPACE_CACHE_DIR", "eval/outputs/fresh-cache")

    assert get_settings().eval_workspace_cache_dir == "eval/outputs/fresh-cache"


def test_eval_performance_defaults_are_bounded(monkeypatch) -> None:
    monkeypatch.delenv("EVAL_CONCURRENCY", raising=False)
    monkeypatch.delenv("EVAL_FIXTURE_CONCURRENCY", raising=False)
    monkeypatch.delenv("REVIEW_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", raising=False)

    settings = get_settings()

    assert settings.eval_concurrency == 1
    assert settings.eval_fixture_concurrency == 3
    assert settings.review_max_iterations == 16
    assert settings.eval_review_max_iterations == 16
    assert settings.eval_review_max_iterations_cap == 16
    assert settings.eval_review_min_tool_iterations == 1
    assert settings.pre_budget_submit_token_ratio == 0.80


def test_token_hard_budget_is_not_below_soft_budget(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_BUDGET", "12000")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "8000")

    settings = get_settings()

    assert settings.token_budget == 12_000
    assert settings.token_hard_budget == 12_000


def test_final_submit_budgets_are_normalized_to_runtime_limits(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_BUDGET", "8000")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "10000")
    monkeypatch.setenv("PROMPT_INPUT_TOKEN_BUDGET", "3000")
    monkeypatch.setenv("FINAL_SUBMIT_RESERVE_TOKENS", "12000")
    monkeypatch.setenv("FINAL_SUBMIT_PROMPT_TOKEN_BUDGET", "4000")
    monkeypatch.setenv("FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET", "5000")

    settings = get_settings()

    assert settings.final_submit_reserve_tokens == 10_000
    assert settings.final_submit_prompt_token_budget == 3_000
    assert settings.final_submit_feedback_token_budget == 2_999


def test_github_advisory_publish_defaults_are_safe(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ADVISORY_DRY_RUN", raising=False)
    monkeypatch.delenv("GITHUB_ADVISORY_COMMENT_MARKER", raising=False)

    settings = get_settings()

    assert settings.github_advisory_dry_run is True
    assert settings.github_advisory_comment_marker == "<!-- mergewarden:comment -->"


def test_github_auth_mode_defaults_to_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    monkeypatch.delenv("GITHUB_APP_MODE", raising=False)

    settings = get_settings()

    assert settings.github_auth_mode == "token"


def test_github_app_mode_switch_and_private_key_newlines(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    monkeypatch.setenv("GITHUB_APP_MODE", "true")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN-----\\nabc\\n-----END-----")

    settings = get_settings()

    assert settings.github_auth_mode == "app"
    assert settings.github_private_key == "-----BEGIN-----\nabc\n-----END-----"


def test_v020_quality_and_recovery_defaults_are_enforced(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_WORKFLOW_ENFORCEMENT", raising=False)
    monkeypatch.delenv("RUN_CHECKPOINTS_ENABLED", raising=False)
    monkeypatch.delenv("RUN_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("RUN_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.delenv("PLATFORM_ARTIFACT_RETENTION_DAYS", raising=False)

    settings = get_settings()

    assert settings.review_workflow_enforcement == "enforce"
    assert settings.run_checkpoints_enabled is True
    assert settings.run_lease_seconds == 180
    assert settings.run_heartbeat_seconds == 30
    assert settings.platform_artifact_retention_days == 30
    assert not hasattr(settings, "finding_verifier_mode")
    assert not hasattr(settings, "verifier_max_repair_rounds")


def test_runtime_version_is_v020() -> None:
    from src import __version__

    assert __version__ == "0.2.0"
