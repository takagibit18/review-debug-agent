"""Read-only static symbol context tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolSafety, ToolSpec
from src.tools.path_utils import ensure_path_allowed
from src.tools.review_context import ReviewToolContext
from src.tools.symbol_backends import StaticSymbolBackend, SymbolBackend, SymbolRecord, SymbolReference


class FindSymbolContextInput(BaseModel):
    """Validated input for static symbol lookup."""

    symbol: str = Field(
        ...,
        min_length=1,
        description="Name of the symbol to look up, such as a class name, function name, "
        "method name, field name, or variable name",
    )
    path: str = Field(
        default=".",
        description="File or directory path that scopes the lookup; use the changed file "
        "path to search within that file, or '.' to search the whole repository",
    )
    mode: Literal["definition", "references", "enclosing", "all"] = Field(
        default="all",
        description="What to return: 'definition' for where the symbol is declared, "
        "'references' for where it is called or used, 'enclosing' for the class or "
        "function that contains a given line, or 'all' for everything at once",
    )
    line: int | None = Field(
        default=None,
        ge=1,
        description="Line number used with mode='enclosing' to find which class or "
        "function contains this line",
    )
    max_results: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Maximum number of definition or reference results to return",
    )
    context_radius: int = Field(
        default=4,
        ge=0,
        le=40,
        description="Number of source lines to include before and after each result "
        "so you can see surrounding code without a separate read_file call",
    )


class FindSymbolContextTool(BaseTool):
    """Find static definitions, references, and enclosing symbols."""

    def __init__(
        self,
        review_context_or_repo_root: ReviewToolContext | Path | str,
        *,
        backend: SymbolBackend | None = None,
    ) -> None:
        if isinstance(review_context_or_repo_root, ReviewToolContext):
            self._repo_root = review_context_or_repo_root.repo_root
        else:
            self._repo_root = Path(review_context_or_repo_root).resolve()
        self._backend = backend or StaticSymbolBackend(self._repo_root)

    def spec(self) -> ToolSpec:
        """Return the LLM-facing tool specification."""
        return ToolSpec(
            name="find_symbol_context",
            description=(
                "Find where a symbol (class, function, method, field, variable) is defined "
                "and referenced across the repository using static analysis. Returns "
                "definition sites with their signatures, call sites and usage references "
                "with surrounding source context, and the enclosing class or function for "
                "a given line. Use this when you have identified a changed symbol and need "
                "to understand who calls it, where it is defined, or what downstream code "
                "might be affected by the change. Prefer this over grep_files when you want "
                "semantic symbol-level results (definitions and references with confidence "
                "scores) rather than raw text matches. For languages where static "
                "analysis returns low confidence, fall back to grep_files for "
                "reliable text-based search. This tool does not start a language "
                "server and does not modify files."
            ),
            parameters=FindSymbolContextInput.model_json_schema(),
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return static symbol context."""
        data = FindSymbolContextInput(**kwargs)
        scope = ensure_path_allowed(Path(data.path), tool_name=self.spec().name)
        language = (
            self._backend.language_for_path(scope)  # type: ignore[attr-defined]
            if scope.is_file() and hasattr(self._backend, "language_for_path")
            else "unknown"
        )
        warnings: list[str] = []
        if language == "unknown" and scope.is_file():
            warnings.append("static fallback used for unsupported language")

        definitions: list[SymbolRecord] = []
        references: list[SymbolReference] = []
        enclosing: list[SymbolRecord] = []
        truncated = False

        if data.mode in {"definition", "all"}:
            definitions = self._backend.find_definitions(data.symbol, scope)
            if len(definitions) > data.max_results:
                definitions = definitions[: data.max_results]
                truncated = True
        if data.mode in {"references", "all"}:
            found = self._backend.find_references(data.symbol, scope, data.max_results + 1)
            if len(found) > data.max_results:
                truncated = True
                found = found[: data.max_results]
            references = found
        if data.mode in {"enclosing", "all"} and data.line is not None:
            enclosing = self._backend.enclosing_symbol(scope, data.line)

        return {
            "backend": getattr(self._backend, "name", "unknown"),
            "language": language,
            "symbol": data.symbol,
            "definitions": [
                self._record_payload(item, data.context_radius) for item in definitions
            ],
            "references": [
                self._reference_payload(item, data.context_radius) for item in references
            ],
            "enclosing_symbols": [
                self._record_payload(item, data.context_radius) for item in enclosing
            ],
            "truncated": truncated,
            "warnings": warnings,
        }

    def _record_payload(self, record: SymbolRecord, radius: int) -> dict[str, Any]:
        return {
            "path": record.path,
            "line": record.line,
            "end_line": record.end_line,
            "kind": record.kind,
            "name": record.name,
            "signature": record.signature,
            "context": self._render_context(record.path, record.line, record.end_line, radius),
            "confidence": record.confidence,
        }

    def _reference_payload(self, reference: SymbolReference, radius: int) -> dict[str, Any]:
        return {
            "path": reference.path,
            "line": reference.line,
            "line_text": reference.line_text,
            "context": self._render_context(reference.path, reference.line, reference.line, radius),
            "confidence": reference.confidence,
        }

    def _render_context(self, path: str, line: int, end_line: int, radius: int) -> str:
        resolved = (self._repo_root / path).resolve()
        try:
            lines = resolved.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ""
        start = max(1, line - radius)
        end = min(len(lines), end_line + radius)
        return "\n".join(
            f"{line_number}: {lines[line_number - 1]}"
            for line_number in range(start, end + 1)
        )
