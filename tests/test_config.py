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

    assert settings.execute_docker_image == "cr-debug-agent-execute:latest"
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
