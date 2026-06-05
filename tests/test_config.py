"""Tests for configuration normalization and validation."""

from __future__ import annotations

from src.config import get_settings


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
    monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MODEL_MAX_RETRIES", raising=False)
    monkeypatch.delenv("AGENT_RUN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGENT_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("EVAL_GIT_TIMEOUT_SECONDS", raising=False)

    settings = get_settings()

    assert settings.token_budget == 30_000
    assert settings.token_hard_budget == 36_000
    assert settings.model_max_tokens == 2_048
    assert settings.model_request_timeout_seconds == 90.0
    assert settings.model_max_retries == 1
    assert settings.agent_run_timeout_seconds == 170.0
    assert settings.agent_tool_timeout_seconds == 30.0
    assert settings.eval_git_timeout_seconds == 120.0


def test_eval_performance_defaults_are_bounded(monkeypatch) -> None:
    monkeypatch.delenv("EVAL_CONCURRENCY", raising=False)
    monkeypatch.delenv("EVAL_FIXTURE_CONCURRENCY", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MAX_ITERATIONS_CAP", raising=False)
    monkeypatch.delenv("EVAL_REVIEW_MIN_TOOL_ITERATIONS", raising=False)

    settings = get_settings()

    assert settings.eval_concurrency == 1
    assert settings.eval_fixture_concurrency == 3
    assert settings.eval_review_max_iterations == 2
    assert settings.eval_review_max_iterations_cap == 2
    assert settings.eval_review_min_tool_iterations == 1


def test_token_hard_budget_is_not_below_soft_budget(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_BUDGET", "12000")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "8000")

    settings = get_settings()

    assert settings.token_budget == 12_000
    assert settings.token_hard_budget == 12_000


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
