"""Tests for configuration normalization and validation."""

from __future__ import annotations

from src.config import get_settings


def test_permission_mode_falls_back_to_default_for_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("PERMISSION_MODE", "invalid-mode")

    settings = get_settings()

    assert settings.permission_mode == "default"
