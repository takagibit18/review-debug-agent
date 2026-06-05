"""Unified diff helpers for new-side changed line numbers."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<header>.*)$"
)


@dataclass(frozen=True)
class ParsedDiffHunk:
    """A parsed unified-diff hunk with new-side changed line numbers."""

    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str]
    changed_new_lines: list[int]


def changed_new_lines_by_file(diff_text: str) -> dict[str, set[int]]:
    """Return new-side added line numbers keyed by repo-relative file path."""
    changed: dict[str, set[int]] = {}
    for file_path, hunks in parse_unified_diff_hunks(diff_text).items():
        for hunk in hunks:
            if hunk.changed_new_lines:
                changed.setdefault(file_path, set()).update(hunk.changed_new_lines)
    return changed


def parse_unified_diff_hunks(diff_text: str) -> dict[str, list[ParsedDiffHunk]]:
    """Parse unified-diff hunks keyed by repo-relative file path."""
    hunks_by_file: dict[str, list[ParsedDiffHunk]] = {}
    old_path = ""
    new_path = ""
    current_path = ""
    current_header = ""
    current_lines: list[str] = []
    current_old_start = 0
    current_old_count = 0
    current_new_start = 0
    current_new_count = 0
    current_new_line: int | None = None
    current_changed_new_lines: list[int] = []

    def flush_hunk() -> None:
        nonlocal current_header, current_lines, current_changed_new_lines
        nonlocal current_old_start, current_old_count, current_new_start, current_new_count
        if not current_header or not current_path:
            return
        hunk = ParsedDiffHunk(
            file_path=current_path,
            old_start=current_old_start,
            old_count=current_old_count,
            new_start=current_new_start,
            new_count=current_new_count,
            header=current_header,
            lines=list(current_lines),
            changed_new_lines=list(current_changed_new_lines),
        )
        hunks_by_file.setdefault(current_path, []).append(hunk)
        current_header = ""
        current_lines = []
        current_changed_new_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush_hunk()
            old_path = ""
            new_path = ""
            current_path = ""
            current_new_line = None
            continue
        if line.startswith("--- "):
            old_path = normalize_diff_path(line[4:])
            if not new_path:
                current_path = old_path
            continue
        if line.startswith("+++ "):
            new_path = normalize_diff_path(line[4:])
            current_path = new_path or old_path
            continue
        if line.startswith("@@"):
            flush_hunk()
            match = _HUNK_RE.match(line)
            if match is None:
                current_header = line
                current_old_start = 0
                current_old_count = 0
                current_new_start = 0
                current_new_count = 0
                current_new_line = None
                continue
            current_header = line
            current_old_start = int(match.group("old_start"))
            current_old_count = int(match.group("old_count") or "1")
            current_new_start = int(match.group("new_start"))
            current_new_count = int(match.group("new_count") or "1")
            current_new_line = current_new_start
            current_lines = []
            current_changed_new_lines = []
            continue
        if not current_header:
            continue
        current_lines.append(line)
        if line.startswith("\\") or current_new_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current_new_line > 0:
                current_changed_new_lines.append(current_new_line)
            current_new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            continue
        current_new_line += 1

    flush_hunk()
    return hunks_by_file


def normalize_diff_path(path: str) -> str:
    """Normalize a unified-diff file header path to a repo-relative path."""
    raw = path.strip().split("\t", 1)[0].strip()
    if not raw or raw == "/dev/null":
        return ""
    try:
        parsed = shlex.split(raw)
    except ValueError:
        parsed = []
    if parsed:
        raw = parsed[0]
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw.replace("\\", "/") if raw != "/dev/null" else ""
