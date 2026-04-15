"""Parse PR diff data into fixture-ready input payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass

from eval.schemas import FixtureInput, FixtureSource

DIFF_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(slots=True)
class HunkRange:
    """One hunk line-range mapping."""

    old_start: int
    old_len: int
    new_start: int
    new_len: int


@dataclass(slots=True)
class DiffFileChange:
    """Per-file diff chunk extracted from unified diff."""

    old_path: str
    new_path: str
    hunks: list[HunkRange]
    raw_patch: str

    @property
    def effective_path(self) -> str:
        if self.new_path != "/dev/null":
            return self.new_path
        return self.old_path


def parse_unified_diff(diff_text: str) -> list[DiffFileChange]:
    """Split unified diff text into file-level changes and hunk ranges."""
    lines = diff_text.splitlines()
    items: list[DiffFileChange] = []

    current_old = ""
    current_new = ""
    current_hunks: list[HunkRange] = []
    current_lines: list[str] = []

    def flush_current() -> None:
        if not current_lines:
            return
        items.append(
            DiffFileChange(
                old_path=current_old,
                new_path=current_new,
                hunks=list(current_hunks),
                raw_patch="\n".join(current_lines).strip(),
            )
        )

    for line in lines:
        file_match = DIFF_FILE_HEADER.match(line)
        if file_match:
            flush_current()
            current_old = file_match.group(1)
            current_new = file_match.group(2)
            current_hunks = []
            current_lines = [line]
            continue

        if not current_lines:
            continue

        current_lines.append(line)
        hunk_match = HUNK_HEADER.match(line)
        if hunk_match:
            current_hunks.append(
                HunkRange(
                    old_start=int(hunk_match.group(1)),
                    old_len=int(hunk_match.group(2) or "1"),
                    new_start=int(hunk_match.group(3)),
                    new_len=int(hunk_match.group(4) or "1"),
                )
            )

    flush_current()
    return items


def build_fixture_input(diff_text: str, file_contents: dict[str, str]) -> FixtureInput:
    """Create `FixtureInput` from diff and fetched file snapshots."""
    parsed = parse_unified_diff(diff_text)
    relevant_paths = {item.effective_path for item in parsed if item.effective_path}
    files = {path: file_contents.get(path, "") for path in sorted(relevant_paths)}
    return FixtureInput(diff_text=diff_text, files=files)


def build_fixture_source(
    *,
    repo_full_name: str,
    pr_number: int,
    url: str,
    merge_commit_sha: str,
    title: str,
) -> FixtureSource:
    """Build fixture source metadata from PR-level fields."""
    return FixtureSource(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        url=url,
        merge_commit_sha=merge_commit_sha,
        title=title,
    )

