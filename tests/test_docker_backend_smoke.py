"""Optional smoke tests for the real Docker execute backend."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from src.security.sandbox import run_sandboxed_command
from src.tools.path_utils import tool_workspace_root

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_TESTS") != "1",
    reason="Docker smoke tests are opt-in; set RUN_DOCKER_TESTS=1 to enable.",
)


def test_run_sandboxed_command_executes_inside_docker() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    if shutil.which("docker") is None:
        pytest.fail("RUN_DOCKER_TESTS=1 was set, but docker is not available on PATH.")

    with tool_workspace_root(repo_root):
        result = run_sandboxed_command(
            argv=["pytest", "-q", "tests/test_config.py"],
            cwd=repo_root,
            timeout_ms=120_000,
            backend="docker",
        )

    assert result.exit_code == 0, result.stderr
    assert "2 passed" in result.stdout or "3 passed" in result.stdout
