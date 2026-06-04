"""Read-only changed hunk and surrounding file context tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.path_utils import ensure_path_allowed
from src.tools.review_context import DiffHunk, ReviewToolContext
from src.tools.symbol_backends import StaticSymbolBackend


class GetChangedContextInput(BaseModel):
    """Validated input for changed context lookup."""

    file_path: str = Field(..., description="Repo-relative path to the changed file to inspect")
    line: int | None = Field(
        default=None,
        ge=1,
        description="New-side line number to center the source window on; use the changed "
        "line numbers from the diff hunk header",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional end line to inspect a range of changed lines",
    )
    hunk_index: int | None = Field(
        default=None,
        ge=0,
        description="Zero-based index of the diff hunk to return; use this instead of line "
        "when you want a specific hunk by its position in the diff",
    )
    radius: int = Field(
        default=40,
        ge=1,
        le=200,
        description="Number of source lines to include before and after the target line "
        "or hunk in the returned file window",
    )
    include_imports: bool = Field(
        default=True,
        description="Whether to return the file's top import/use/using statements so you "
        "can see module dependencies without an extra read_file call",
    )
    include_enclosing_symbol: bool = Field(
        default=True,
        description="Whether to return the enclosing class, function, or method symbol "
        "at the requested line so you know the structural context",
    )


class GetChangedContextTool(BaseTool):
    """Return a diff hunk, changed lines, imports, symbols, and nearby file lines."""

    def __init__(self, review_context: ReviewToolContext) -> None:
        self._context = review_context
        self._symbol_backend = StaticSymbolBackend(review_context.repo_root)

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="get_changed_context",
            description=(
                "Inspect the diff hunks and surrounding source code of a changed file. "
                "Returns the matching diff hunk (added and removed lines), the exact changed "
                "line numbers, a window of nearby source lines, the file's top imports, and "
                "the enclosing class or function symbol. Use this when you need to understand "
                "what a diff hunk actually changed and see the code around it before deciding "
                "whether it introduces a bug or regression. Prefer this over read_file when "
                "you already have a diff and want the changed region with its immediate "
                "context — it packages the diff hunk and source window together so you do "
                "not need multiple read_file calls. This tool only inspects files that "
                "appear in the diff. Use read_file when you need to examine unchanged "
                "files that may be affected by the change. It does not search for "
                "patterns or symbols across files."
            ),
            parameters=GetChangedContextInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return changed context for one file location or hunk index."""
        data = GetChangedContextInput(**kwargs)
        resolved = ensure_path_allowed(Path(data.file_path), tool_name=self.spec().name)
        rel_path = self._repo_relative(resolved)
        warnings: list[str] = []
        hunks = self._context.diff_hunks_by_file.get(rel_path, [])
        selected_hunk = self._select_hunk(hunks, data)
        if selected_hunk is None and hunks:
            warnings.append("requested location is outside changed lines")
        if not hunks:
            warnings.append("file has no diff hunks in review context")

        lines = self._read_lines(resolved, warnings)
        start_line, end_line = self._window_bounds(data, selected_hunk, len(lines))
        enclosing_symbols: list[dict[str, Any]] = []
        if data.include_enclosing_symbol and data.line is not None and resolved.exists():
            enclosing_symbols = [
                {
                    "path": item.path,
                    "line": item.line,
                    "end_line": item.end_line,
                    "kind": item.kind,
                    "name": item.name,
                    "signature": item.signature,
                    "confidence": item.confidence,
                }
                for item in self._symbol_backend.enclosing_symbol(resolved, data.line)
            ]

        return {
            "file_path": rel_path,
            "requested_location": {
                "line": data.line,
                "end_line": data.end_line,
                "hunk_index": data.hunk_index,
            },
            "in_changed_hunk": selected_hunk is not None,
            "hunk": self._hunk_payload(selected_hunk, hunks) if selected_hunk else None,
            "file_window": {
                "start_line": start_line,
                "end_line": end_line,
                "content": self._render_window(lines, start_line, end_line),
                "truncated_before": start_line > 1,
                "truncated_after": end_line < len(lines),
            },
            "imports_preview": self._imports_preview(lines) if data.include_imports else "",
            "enclosing_symbols": enclosing_symbols,
            "warnings": warnings,
        }

    def _select_hunk(
        self,
        hunks: list[DiffHunk],
        data: GetChangedContextInput,
    ) -> DiffHunk | None:
        if data.hunk_index is not None:
            if 0 <= data.hunk_index < len(hunks):
                return hunks[data.hunk_index]
            return None
        if data.line is None:
            return None
        end_line = data.end_line or data.line
        requested_lines = range(data.line, end_line + 1)
        for hunk in hunks:
            changed = set(hunk.changed_new_lines)
            if any(line in changed for line in requested_lines):
                return hunk
        return None

    def _window_bounds(
        self,
        data: GetChangedContextInput,
        hunk: DiffHunk | None,
        total_lines: int,
    ) -> tuple[int, int]:
        if total_lines <= 0:
            return 1, 0
        if data.line is not None:
            start_target = data.line
            end_target = data.end_line or data.line
        elif hunk and hunk.changed_new_lines:
            start_target = min(hunk.changed_new_lines)
            end_target = max(hunk.changed_new_lines)
        elif hunk:
            start_target = hunk.new_start
            end_target = max(hunk.new_start, hunk.new_start + hunk.new_count - 1)
        else:
            start_target = 1
            end_target = 1
        start_line = max(1, start_target - data.radius)
        end_line = min(total_lines, end_target + data.radius)
        return start_line, end_line

    def _hunk_payload(self, hunk: DiffHunk, all_hunks: list[DiffHunk]) -> dict[str, Any]:
        return {
            "index": all_hunks.index(hunk),
            "header": hunk.header,
            "new_start": hunk.new_start,
            "new_count": hunk.new_count,
            "changed_new_lines": list(hunk.changed_new_lines),
            "text": hunk.text(),
        }

    def _read_lines(self, path: Path, warnings: list[str]) -> list[str]:
        try:
            return path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            warnings.append("file not found on disk; returning diff-only context")
            return []
        except OSError:
            warnings.append("file could not be read; returning diff-only context")
            return []

    @staticmethod
    def _render_window(lines: list[str], start_line: int, end_line: int) -> str:
        if end_line < start_line:
            return ""
        return "\n".join(
            f"{line_number}: {lines[line_number - 1]}"
            for line_number in range(start_line, end_line + 1)
        )

    @staticmethod
    def _imports_preview(lines: list[str]) -> str:
        imports: list[str] = []
        for line in lines[:80]:
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "use ", "using ")):
                imports.append(line)
                continue
            if not stripped and imports:
                break
            if not stripped and not imports:
                continue
            if imports:
                break
        return "\n".join(imports)

    def _repo_relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._context.repo_root).as_posix()
        except ValueError:
            return path.as_posix()
