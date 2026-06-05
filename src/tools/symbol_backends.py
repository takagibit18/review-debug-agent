"""Symbol lookup backends for review context tools."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SymbolRecord:
    """A static symbol definition or enclosing symbol range."""

    path: str
    line: int
    end_line: int
    kind: str
    name: str
    signature: str
    confidence: float


@dataclass(frozen=True)
class SymbolReference:
    """A symbol reference with source line metadata."""

    path: str
    line: int
    line_text: str
    confidence: float


class SymbolBackend(Protocol):
    """Protocol that can later be implemented by an LSP backend."""

    name: str

    def document_symbols(self, path: Path) -> list[SymbolRecord]:
        """Return symbols declared in a document."""

    def find_definitions(self, symbol: str, path: Path | None = None) -> list[SymbolRecord]:
        """Return candidate definitions for a symbol."""

    def find_references(
        self,
        symbol: str,
        path: Path | None = None,
        max_results: int = 50,
    ) -> list[SymbolReference]:
        """Return references for a symbol."""

    def enclosing_symbol(self, path: Path, line: int) -> list[SymbolRecord]:
        """Return symbols that enclose a line."""


class StaticSymbolBackend:
    """Lightweight static symbol backend; it does not use LSP or external servers."""

    name = "static"

    _CS_TYPE_PATTERN = re.compile(r"\b(class|interface|struct|enum)\s+([A-Za-z_]\w*)")
    _CS_FIELD_PATTERN = re.compile(
        r"\b(?:public|private|protected|internal)\s+(?:static\s+)?(?:readonly\s+)?"
        r"[A-Za-z_][\w<>\[\],.?]*\s+([A-Za-z_]\w*)\s*(?:[;=])"
    )
    _CS_METHOD_PATTERN = re.compile(
        r"\b(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?"
        r"[A-Za-z_][\w<>\[\],.?]*\s+([A-Za-z_]\w*)\s*\("
    )
    _CS_CTOR_PATTERN = re.compile(r"\b(?:public|private|protected|internal)\s+([A-Za-z_]\w*)\s*\(")
    _RS_PATTERN = re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?P<kind>fn|struct|enum|trait|mod)\s+"
        r"(?P<name>[A-Za-z_]\w*)"
    )
    _RS_IMPL_PATTERN = re.compile(r"^\s*impl(?:<[^>]+>)?\s+(?P<name>[A-Za-z_]\w*)")

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root).resolve()

    def document_symbols(self, path: Path) -> list[SymbolRecord]:
        resolved = self._resolve(path)
        language = self.language_for_path(resolved)
        if language == "python":
            return self._python_symbols(resolved)
        if language == "rust":
            return self._rust_symbols(resolved)
        if language == "csharp":
            return self._csharp_symbols(resolved)
        return []

    def find_definitions(self, symbol: str, path: Path | None = None) -> list[SymbolRecord]:
        candidates = self._candidate_files(path)
        matches: list[SymbolRecord] = []
        for candidate in candidates:
            language = self.language_for_path(candidate)
            if language == "unknown":
                matches.extend(self._fallback_definition_matches(symbol, candidate))
                continue
            matches.extend(
                record for record in self.document_symbols(candidate) if record.name == symbol
            )
        return sorted(matches, key=lambda item: (item.path, item.line))

    def find_references(
        self,
        symbol: str,
        path: Path | None = None,
        max_results: int = 50,
    ) -> list[SymbolReference]:
        if not symbol:
            return []
        regex = re.compile(rf"\b{re.escape(symbol)}\b")
        references: list[SymbolReference] = []
        for candidate in self._candidate_files(path):
            language = self.language_for_path(candidate)
            confidence = 0.35 if language == "unknown" else 0.65
            try:
                lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            rel = self._relative(candidate)
            for line_number, line_text in enumerate(lines, start=1):
                if regex.search(line_text) is None:
                    continue
                references.append(
                    SymbolReference(
                        path=rel,
                        line=line_number,
                        line_text=line_text,
                        confidence=confidence,
                    )
                )
                if len(references) >= max_results:
                    return references
        return references

    def enclosing_symbol(self, path: Path, line: int) -> list[SymbolRecord]:
        records = [
            record
            for record in self.document_symbols(path)
            if record.line <= line <= max(record.line, record.end_line)
        ]
        return sorted(records, key=lambda item: (item.line, item.end_line))

    def language_for_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix == ".rs":
            return "rust"
        if suffix == ".cs":
            return "csharp"
        return "unknown"

    def _python_symbols(self, path: Path) -> list[SymbolRecord]:
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            return []
        lines = source.splitlines()
        records: list[SymbolRecord] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                records.append(self._python_record(path, lines, node, "class", node.name))
            elif isinstance(node, ast.AsyncFunctionDef):
                records.append(self._python_record(path, lines, node, "function", node.name))
            elif isinstance(node, ast.FunctionDef):
                records.append(self._python_record(path, lines, node, "function", node.name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[-1]
                    records.append(self._python_record(path, lines, node, "import", name))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        records.append(self._python_record(path, lines, node, "assign", target.id))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                records.append(self._python_record(path, lines, node, "assign", node.target.id))
        return sorted(records, key=lambda item: (item.line, item.end_line, item.name))

    def _python_record(
        self,
        path: Path,
        lines: list[str],
        node: ast.AST,
        kind: str,
        name: str,
    ) -> SymbolRecord:
        line = int(getattr(node, "lineno", 1))
        end_line = int(getattr(node, "end_lineno", line))
        return SymbolRecord(
            path=self._relative(path),
            line=line,
            end_line=end_line,
            kind=kind,
            name=name,
            signature=lines[line - 1].strip() if 0 <= line - 1 < len(lines) else name,
            confidence=0.9,
        )

    def _rust_symbols(self, path: Path) -> list[SymbolRecord]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        records: list[SymbolRecord] = []
        for index, line in enumerate(lines, start=1):
            impl_match = self._RS_IMPL_PATTERN.search(line)
            if impl_match:
                records.append(
                    self._line_record(path, index, "impl", impl_match.group("name"), line, 0.75)
                )
                continue
            match = self._RS_PATTERN.search(line)
            if match is None:
                if line.strip().startswith("use "):
                    name = line.strip().removeprefix("use ").rstrip(";").split("::")[-1]
                    records.append(self._line_record(path, index, "use", name, line, 0.75))
                continue
            kind = "function" if match.group("kind") == "fn" else match.group("kind")
            records.append(self._line_record(path, index, kind, match.group("name"), line, 0.8))
        return records

    def _csharp_symbols(self, path: Path) -> list[SymbolRecord]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        records: list[SymbolRecord] = []
        class_names: set[str] = set()
        for index, line in enumerate(lines, start=1):
            type_match = self._CS_TYPE_PATTERN.search(line)
            if type_match:
                name = type_match.group(2)
                class_names.add(name)
                records.append(self._line_record(path, index, type_match.group(1), name, line, 0.8))
            if line.strip().startswith("using "):
                name = line.strip().removeprefix("using ").rstrip(";").split(".")[-1]
                records.append(self._line_record(path, index, "using", name, line, 0.75))
        for index, line in enumerate(lines, start=1):
            ctor_match = self._CS_CTOR_PATTERN.search(line)
            if ctor_match and ctor_match.group(1) in class_names:
                records.append(self._line_record(path, index, "constructor", ctor_match.group(1), line, 0.8))
                continue
            method_match = self._CS_METHOD_PATTERN.search(line)
            if method_match:
                records.append(self._line_record(path, index, "method", method_match.group(1), line, 0.75))
                continue
            if "(" not in line:
                field_match = self._CS_FIELD_PATTERN.search(line)
                if field_match:
                    records.append(self._line_record(path, index, "field", field_match.group(1), line, 0.75))
        return sorted(records, key=lambda item: (item.line, item.kind, item.name))

    def _fallback_definition_matches(self, symbol: str, path: Path) -> list[SymbolRecord]:
        references = self.find_references(symbol, path, max_results=20)
        return [
            SymbolRecord(
                path=reference.path,
                line=reference.line,
                end_line=reference.line,
                kind="text",
                name=symbol,
                signature=reference.line_text.strip(),
                confidence=0.35,
            )
            for reference in references
        ]

    def _line_record(
        self,
        path: Path,
        line: int,
        kind: str,
        name: str,
        signature: str,
        confidence: float,
    ) -> SymbolRecord:
        return SymbolRecord(
            path=self._relative(path),
            line=line,
            end_line=line,
            kind=kind,
            name=name,
            signature=signature.strip(),
            confidence=confidence,
        )

    def _candidate_files(self, path: Path | None) -> list[Path]:
        root = self._resolve(path or self.repo_root)
        if root.is_file():
            return [root]
        if not root.exists() or not root.is_dir():
            return []
        candidates: list[Path] = []
        for candidate in root.rglob("*"):
            if len(candidates) >= 500:
                break
            if not candidate.is_file():
                continue
            if any(part.startswith(".") for part in candidate.relative_to(root).parts):
                continue
            candidates.append(candidate)
        return sorted(candidates, key=lambda item: str(item))

    def _resolve(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.repo_root / path).resolve()

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()
