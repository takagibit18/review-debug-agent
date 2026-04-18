"""Tests for command allowlist / argv policy."""

from __future__ import annotations

import pytest

from src.security.exec_policy import (
    resolve_command,
    truncate_output,
    validate_extra_args,
)
from src.tools.exceptions import CommandNotAllowedError

_ALLOWED = ("python", "pytest", "git", "ruff")


def test_resolve_command_accepts_allowlisted_head() -> None:
    assert resolve_command("pytest -q tests", allowed=_ALLOWED) == ["pytest", "-q", "tests"]


def test_resolve_command_rejects_empty() -> None:
    with pytest.raises(CommandNotAllowedError):
        resolve_command("   ", allowed=_ALLOWED)


def test_resolve_command_rejects_non_allowlisted() -> None:
    with pytest.raises(CommandNotAllowedError):
        resolve_command("rm -rf /", allowed=_ALLOWED)


def test_resolve_command_accepts_git_readonly_subcommand() -> None:
    argv = resolve_command("git status", allowed=_ALLOWED)
    assert argv == ["git", "status"]


def test_resolve_command_rejects_git_write_subcommand() -> None:
    with pytest.raises(CommandNotAllowedError):
        resolve_command("git push origin main", allowed=_ALLOWED)


def test_resolve_command_rejects_shell_operators() -> None:
    # shlex.split turns "&&" / "|" into standalone tokens; policy rejects them.
    with pytest.raises(CommandNotAllowedError):
        resolve_command("pytest && rm -rf /", allowed=_ALLOWED)
    with pytest.raises(CommandNotAllowedError):
        resolve_command("pytest | grep foo", allowed=_ALLOWED)


def test_resolve_command_rejects_chained_head_via_operator() -> None:
    # "curl; pytest" — head is curl which is not allowlisted.
    with pytest.raises(CommandNotAllowedError):
        resolve_command("curl http://x; pytest", allowed=_ALLOWED)


def test_validate_extra_args_rejects_network_flag() -> None:
    with pytest.raises(CommandNotAllowedError):
        validate_extra_args(["--network=host"])


def test_validate_extra_args_rejects_inline_c_exec() -> None:
    with pytest.raises(CommandNotAllowedError):
        validate_extra_args(["-c", "print(1)"])
    with pytest.raises(CommandNotAllowedError):
        validate_extra_args(["-cimport os"])


def test_validate_extra_args_passes_normal_flags() -> None:
    assert validate_extra_args(["-q", "tests/test_x.py"]) == ["-q", "tests/test_x.py"]


def test_truncate_output_short_text_unchanged() -> None:
    text, was = truncate_output("hello", 100)
    assert text == "hello"
    assert was is False


def test_truncate_output_long_text_marked() -> None:
    payload = "a" * 200
    text, was = truncate_output(payload, 50)
    assert was is True
    assert text.endswith("[truncated]")
    assert text.startswith("a" * 50)
