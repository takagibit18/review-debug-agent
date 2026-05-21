"""Unified diff helpers for new-side changed line numbers."""

from __future__ import annotations

import re
import shlex

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_new_lines_by_file(diff_text: str) -> dict[str, set[int]]:
    """Return new-side added line numbers keyed by repo-relative file path."""
    changed: dict[str, set[int]] = {}
    current_path = ""
    new_line: int | None = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = ""
            new_line = None
            continue
        if line.startswith("+++ "):
            current_path = normalize_diff_path(line[4:])
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.search(line)
            new_line = int(match.group(1)) if match else None
            continue
        if line.startswith("\\") or not current_path or new_line is None:
            continue
        if line.startswith("+"):
            if new_line > 0:
                changed.setdefault(current_path, set()).add(new_line)
            new_line += 1
            continue
        if line.startswith("-"):
            continue
        if line.startswith(" "):
            new_line += 1

    return changed


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
