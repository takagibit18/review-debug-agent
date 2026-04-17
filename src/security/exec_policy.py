"""Command parsing and allowlist policy for execute-class tools.

Converts user-provided shell-like strings into explicit argv lists and
validates them against a first-token allowlist. Shell interpretation is
deliberately disabled in the backend; chained operators (&&, |, redirects)
therefore become ordinary tokens that will fail the allowlist check.
"""

from __future__ import annotations

import shlex
from typing import Iterable


def _not_allowed(message: str, *, tool_name: str) -> Exception:
    # Local import keeps src.security independent of src.tools package init.
    from src.tools.exceptions import CommandNotAllowedError

    return CommandNotAllowedError(message, tool_name=tool_name)

_GIT_SUBCOMMANDS_READONLY: frozenset[str] = frozenset(
    {"status", "diff", "log", "show", "rev-parse"}
)
_FORBIDDEN_TOKENS: frozenset[str] = frozenset(
    {"&&", "||", "|", ";", ">", ">>", "<", "`", "$("}
)
_FORBIDDEN_EXTRA_PREFIXES: tuple[str, ...] = ("--network", "--privileged")

_TOOL_NAME_DEFAULT = "run_command"


def resolve_command(
    command: str,
    *,
    allowed: Iterable[str],
    tool_name: str = _TOOL_NAME_DEFAULT,
) -> list[str]:
    """Parse command string and enforce allowlist rules.

    Returns a validated argv list suitable for ``subprocess.run(..., shell=False)``.
    Raises :class:`CommandNotAllowedError` when rejected.
    """
    if not isinstance(command, str):
        raise _not_allowed("Command must be a string", tool_name=tool_name
        )
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise _not_allowed(f"Failed to parse command: {exc}", tool_name=tool_name
        ) from exc
    if not argv:
        raise _not_allowed("Empty command", tool_name=tool_name)

    allowed_set = {item for item in allowed if item}
    head = argv[0]
    if head not in allowed_set:
        raise _not_allowed(f"Command '{head}' is not in allowlist",
            tool_name=tool_name,
        )

    for token in argv[1:]:
        if token in _FORBIDDEN_TOKENS:
            raise _not_allowed(f"Forbidden shell token in arguments: {token!r}",
                tool_name=tool_name,
            )

    if head == "git":
        if len(argv) < 2 or argv[1] not in _GIT_SUBCOMMANDS_READONLY:
            raise _not_allowed(f"git subcommand not allowed: {argv[1:]}",
                tool_name=tool_name,
            )

    return argv


def validate_extra_args(
    extra_args: Iterable[str], *, tool_name: str = "run_tests"
) -> list[str]:
    """Validate additional arguments (as a pre-split list) for execute tools."""
    result: list[str] = []
    for raw in extra_args:
        token = str(raw)
        if token in _FORBIDDEN_TOKENS:
            raise _not_allowed(f"Forbidden shell token in extra args: {token!r}",
                tool_name=tool_name,
            )
        for prefix in _FORBIDDEN_EXTRA_PREFIXES:
            if token == prefix or token.startswith(prefix + "="):
                raise _not_allowed(f"Forbidden argument: {token!r}",
                    tool_name=tool_name,
                )
        if token.startswith("-c") and len(token) > 2:
            raise _not_allowed(f"Inline -c execution is not allowed: {token!r}",
                tool_name=tool_name,
            )
        if token == "-c":
            raise _not_allowed("Inline -c execution is not allowed",
                tool_name=tool_name,
            )
        result.append(token)
    return result


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Truncate text to at most ``limit`` UTF-8 bytes.

    Returns ``(possibly_truncated_text, was_truncated)``.
    """
    if limit <= 0:
        return "", bool(text)
    data = text.encode("utf-8", errors="ignore")
    if len(data) <= limit:
        return text, False
    truncated = data[:limit].decode("utf-8", errors="ignore")
    return truncated + "\n...[truncated]", True

