"""Shared pytest fixtures."""

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _disable_v020_model_gates_by_default(monkeypatch):  # type: ignore[no-untyped-def]
    """Keep legacy tests focused on their original model-call contracts."""
    monkeypatch.setenv("FINDING_VERIFIER_MODE", "off")
    monkeypatch.setenv("REVIEW_WORKFLOW_ENFORCEMENT", "off")


@pytest.fixture
def cli_runner() -> CliRunner:
    """Provide a Click CLI test runner."""
    return CliRunner()
