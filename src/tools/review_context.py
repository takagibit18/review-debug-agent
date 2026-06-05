"""Run-scoped review context shared by advisory review tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.analyzer.diff_lines import ParsedDiffHunk, changed_new_lines_by_file, parse_unified_diff_hunks


@dataclass(frozen=True)
class DiffHunk:
    """Unified-diff hunk data exposed to review tools."""

    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str]
    changed_new_lines: list[int]

    @classmethod
    def from_parsed(cls, hunk: ParsedDiffHunk) -> "DiffHunk":
        return cls(
            file_path=hunk.file_path,
            old_start=hunk.old_start,
            old_count=hunk.old_count,
            new_start=hunk.new_start,
            new_count=hunk.new_count,
            header=hunk.header,
            lines=list(hunk.lines),
            changed_new_lines=list(hunk.changed_new_lines),
        )

    def text(self) -> str:
        """Return hunk text including its header."""
        return "\n".join([self.header, *self.lines])


@dataclass(frozen=True)
class ReviewToolContext:
    """Per-run diff state for review-only atomic evidence tools."""

    repo_root: Path
    diff_text: str
    changed_lines_by_file: dict[str, set[int]]
    diff_hunks_by_file: dict[str, list[DiffHunk]]

    @classmethod
    def from_diff(cls, repo_root: Path | str, diff_text: str) -> "ReviewToolContext":
        """Build context from a repo root and unified diff text."""
        parsed_hunks = parse_unified_diff_hunks(diff_text)
        return cls(
            repo_root=Path(repo_root).resolve(),
            diff_text=diff_text,
            changed_lines_by_file=changed_new_lines_by_file(diff_text),
            diff_hunks_by_file={
                file_path: [DiffHunk.from_parsed(hunk) for hunk in hunks]
                for file_path, hunks in parsed_hunks.items()
            },
        )
