"""Tests for configuration normalization and validation."""

from __future__ import annotations

from src.config import get_settings


def test_permission_mode_falls_back_to_default_for_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("PERMISSION_MODE", "invalid-mode")

    settings = get_settings()

    assert settings.permission_mode == "default"


def test_execute_docker_settings_are_normalized(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTE_BACKEND", "docker")
    monkeypatch.setenv("EXECUTE_DOCKER_IMAGE", "ghcr.io/acme/review-agent:ci")
    monkeypatch.setenv("EXECUTE_DOCKER_WORKDIR", "worktree")
    monkeypatch.setenv("EXECUTE_DOCKER_NETWORK_DISABLED", "false")

    settings = get_settings()

    assert settings.execute_backend == "docker"
    assert settings.execute_docker_image == "ghcr.io/acme/review-agent:ci"
    assert settings.execute_docker_workdir == "/worktree"
    assert settings.execute_docker_network_disabled is False
