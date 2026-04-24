"""Context preparation utilities for the orchestrator prepare phase."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from src.analyzer.context_state import ContextState, DecisionStep
from src.analyzer.schemas import DebugRequest, ReviewRequest

if TYPE_CHECKING:
    from src.analyzer.context_compressor import ContextCompressor

_tiktoken: Any
try:
    import tiktoken as _tiktoken_module

    _tiktoken = _tiktoken_module
except Exception:  # noqa: BLE001
    _tiktoken = None


class ContextPart(BaseModel):
    """One context slice considered for model input."""

    priority: int
    label: str
    content: str
    token_count: int = Field(default=0, ge=0)


class ContextBuilder:
    """Construct run context and prepare model input fragments."""

    _IGNORED_STRUCTURE_NAMES = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".pytest-tmp",
        ".pytest-workspaces",
    }
    _MAX_STRUCTURE_DEPTH = 2
    _MAX_STRUCTURE_ENTRIES = 200
    _MAX_REVIEW_FILES = 6
    _MAX_DEBUG_FILES = 6

    def prepare_context(self, request: ReviewRequest | DebugRequest) -> ContextState:
        goal = "Run structured code review"
        constraints = ["cli_entrypoint"]
        if isinstance(request, DebugRequest):
            goal = "Run structured debug analysis"
        return ContextState(
            goal=goal,
            constraints=constraints,
            decisions=[
                DecisionStep(
                    phase="prepare",
                    action="Initialize context state",
                    result=f"Tracking {request.repo_path}",
                )
            ],
            current_files=[request.repo_path],
        )

    def load_diff(self, repo_path: str) -> str:
        try:
            has_head = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "--verify", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
            if has_head.returncode == 0:
                result = subprocess.run(
                    ["git", "-C", repo_path, "diff", "HEAD", "--"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            for command in (
                ["git", "-C", repo_path, "diff", "--cached", "--"],
                ["git", "-C", repo_path, "diff", "--", "."],
            ):
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            return ""
        except Exception:  # noqa: BLE001
            return ""

    def load_files(self, paths: list[str]) -> dict[str, str]:
        loaded: dict[str, str] = {}
        for raw in paths:
            path = Path(raw)
            if path.is_file():
                try:
                    loaded[str(path)] = path.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    loaded[str(path)] = ""
        return loaded

    def load_repo_files(
        self,
        repo_path: str,
        relative_paths: list[str],
        *,
        limit: int,
    ) -> dict[str, str]:
        root = Path(repo_path).resolve()
        loaded: dict[str, str] = {}
        seen: set[str] = set()
        for rel_path in relative_paths:
            normalized = rel_path.replace("\\", "/").lstrip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidate = (root / normalized).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in content:
                continue
            loaded[normalized] = content
            if len(loaded) >= limit:
                break
        return loaded

    def describe_project_structure(
        self,
        repo_path: str,
        *,
        max_depth: int | None = None,
        max_entries: int | None = None,
    ) -> str:
        root = Path(repo_path).resolve()
        if not root.exists() or not root.is_dir():
            return ""

        depth_limit = max_depth or self._MAX_STRUCTURE_DEPTH
        entry_limit = max_entries or self._MAX_STRUCTURE_ENTRIES
        lines = [
            f"Workspace root: {root}",
            f"Project structure (depth<={depth_limit}, entries<={entry_limit}):",
        ]
        entry_count = 0
        truncated = False

        def walk(directory: Path, depth: int) -> bool:
            nonlocal entry_count, truncated
            if depth > depth_limit:
                return True
            try:
                children = sorted(
                    directory.iterdir(),
                    key=lambda item: (item.is_file(), item.name.lower(), item.name),
                )
            except OSError:
                return True
            for child in children:
                if child.name in self._IGNORED_STRUCTURE_NAMES or child.is_symlink():
                    continue
                indent = "  " * depth
                suffix = "/" if child.is_dir() else ""
                lines.append(f"{indent}- {child.name}{suffix}")
                entry_count += 1
                if entry_count >= entry_limit:
                    truncated = True
                    return False
                if child.is_dir() and depth < depth_limit:
                    if not walk(child, depth + 1):
                        return False
            return True

        walk(root, 0)
        if truncated:
            lines.append("... truncated ...")
        return "\n".join(lines)

    def build_review_file_context(self, repo_path: str, diff_text: str) -> dict[str, str]:
        changed_files = self.extract_changed_files_from_diff(diff_text)
        return self.load_repo_files(
            repo_path,
            changed_files,
            limit=self._MAX_REVIEW_FILES,
        )

    def build_debug_file_context(self, repo_path: str, error_log: str) -> dict[str, str]:
        error_files = self.extract_file_paths_from_error_log(repo_path, error_log)
        return self.load_repo_files(
            repo_path,
            error_files,
            limit=self._MAX_DEBUG_FILES,
        )

    def extract_changed_files_from_diff(self, diff_text: str) -> list[str]:
        if not diff_text.strip():
            return []
        paths: list[str] = []
        seen: set[str] = set()
        for raw_line in diff_text.splitlines():
            line = raw_line.strip()
            if line.startswith("diff --git "):
                try:
                    parts = shlex.split(line)
                except ValueError:
                    parts = line.split()
                if len(parts) >= 4:
                    candidate = self._normalize_diff_path(parts[3])
                    if candidate and candidate not in seen:
                        seen.add(candidate)
                        paths.append(candidate)
                continue
            if line.startswith("+++ "):
                candidate = self._normalize_diff_path(line[4:])
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    paths.append(candidate)
        return paths

    def extract_file_paths_from_error_log(self, repo_path: str, error_log: str) -> list[str]:
        if not error_log.strip():
            return []
        root = Path(repo_path).resolve()
        candidates: list[str] = []
        for match in re.finditer(r'File "([^"]+)"', error_log):
            candidates.append(match.group(1))
        for match in re.finditer(
            r"(?<![A-Za-z0-9_./-])((?:src|tests|app|lib|server|client|scripts|packages)"
            r"[\\/][^:\n\"']+\.[A-Za-z0-9_]+)(?::\d+)?",
            error_log,
        ):
            candidates.append(match.group(1))

        resolved_paths: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = self._normalize_repo_candidate(root, candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                resolved_paths.append(normalized)
        return resolved_paths

    def load_error_log(self, path: str | None, text: str | None) -> str:
        if text:
            return text
        if path:
            try:
                return Path(path).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        if _tiktoken is None:
            return max(1, len(text) // 4)
        encoding = _tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))

    def truncate_context(self, parts: list[ContextPart], budget: int) -> list[ContextPart]:
        if budget <= 0:
            return []
        selected: list[ContextPart] = []
        total = 0
        for part in sorted(parts, key=lambda item: item.priority):
            count = part.token_count or self.estimate_tokens(part.content)
            if total + count > budget:
                continue
            selected.append(
                ContextPart(
                    priority=part.priority,
                    label=part.label,
                    content=part.content,
                    token_count=count,
                )
            )
            total += count
        return selected

    async def truncate_with_summary(
        self,
        parts: list[ContextPart],
        budget: int,
        *,
        compressor: ContextCompressor | None = None,
        model_name: str = "",
        max_summary_tokens: int = 1000,
    ) -> tuple[list[ContextPart], bool]:
        """Two-layer truncation: greedy fit first, then summarize overflowed parts."""
        selected = self.truncate_context(parts, budget)
        selected_labels = {item.label for item in selected}
        discarded = [item for item in parts if item.label not in selected_labels]
        if not discarded or compressor is None:
            return selected, False

        summarized = await compressor.summarize_parts(
            discarded,
            model_name=model_name,
            max_summary_tokens=max_summary_tokens,
        )
        if not summarized:
            return selected, False
        refit = self.truncate_context([*selected, *summarized], budget)
        return refit, True

    @staticmethod
    def _normalize_diff_path(raw_path: str) -> str:
        candidate = raw_path.strip().strip('"').replace("\\", "/")
        if candidate.startswith("a/") or candidate.startswith("b/"):
            candidate = candidate[2:]
        if candidate in {"", "/dev/null"}:
            return ""
        return candidate.lstrip("/")

    @staticmethod
    def _normalize_repo_candidate(root: Path, raw_path: str) -> str:
        if not raw_path.strip():
            return ""
        candidate = Path(raw_path.strip().strip('"'))
        try:
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                resolved = (root / candidate).resolve()
        except OSError:
            return ""
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return ""
        return resolved.relative_to(root).as_posix()
