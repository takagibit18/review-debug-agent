"""Tests for execute-tool mode filtering in the default registry."""

from __future__ import annotations

from src.config import get_settings
from src.tools import create_default_registry


_EXECUTE_NAMES = {"run_command", "run_tests"}


def test_default_registry_excludes_execute_tools() -> None:
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    registry = create_default_registry(include_execute=False)
    names = {spec.name for spec in registry.list_specs()}
    assert _EXECUTE_NAMES.isdisjoint(names)


def test_registry_includes_execute_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTE_ENABLED", "true")
    registry = create_default_registry(include_execute=True)
    names = {spec.name for spec in registry.list_specs()}
    assert _EXECUTE_NAMES.issubset(names)


def test_registry_omits_execute_when_global_switch_off(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTE_ENABLED", "false")
    registry = create_default_registry(include_execute=True)
    names = {spec.name for spec in registry.list_specs()}
    assert _EXECUTE_NAMES.isdisjoint(names)
