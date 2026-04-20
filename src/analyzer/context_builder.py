"""Context preparation utilities for the orchestrator prepare phase."""

from __future__ import annotations

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
            result = subprocess.run(
                ["git", "-C", repo_path, "diff", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return ""
            return result.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    def build_project_structure(
        self,
        repo_path: str,
        *,
        max_depth: int,
        max_entries: int,
    ) -> str:
        root = Path(repo_path).resolve()
        if not root.exists() or not root.is_dir():
            return ""
        lines: list[str] = [f"{root.name}/"]
        emitted = 1
        truncated = False

        def walk(path: Path, depth: int) -> None:
            nonlocal emitted, truncated
            if truncated or depth > max_depth:
                return
            try:
                children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError:
                return
            for child in children:
                if truncated:
                    return
                if child.name.startswith("."):
                    continue
                rel = child.relative_to(root).as_posix()
                indent = "  " * depth
                suffix = "/" if child.is_dir() else ""
                lines.append(f"{indent}- {rel}{suffix}")
                emitted += 1
                if emitted >= max_entries:
                    truncated = True
                    lines.append(f"{indent}- ... (truncated)")
                    return
                if child.is_dir():
                    walk(child, depth + 1)

        walk(root, 1)
        return "\n".join(lines)

    def load_diff_file_contents(
        self,
        repo_path: str,
        diff_text: str,
        *,
        max_files: int,
        max_chars_per_file: int,
        max_chars_total: int,
    ) -> dict[str, str]:
        root = Path(repo_path).resolve()
        if not diff_text.strip() or not root.is_dir():
            return {}
        diff_files = self._extract_diff_paths(diff_text)
        if not diff_files:
            return {}
        selected: list[str] = []
        seen: set[str] = set()
        for rel in diff_files:
            if rel not in seen:
                seen.add(rel)
                selected.append(rel)
        for rel in list(diff_files):
            for neighbor in self._candidate_neighbor_files(rel):
                if len(selected) >= max_files:
                    break
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                selected.append(neighbor)
            if len(selected) >= max_files:
                break

        loaded: dict[str, str] = {}
        total_chars = 0
        for rel in selected:
            if len(loaded) >= max_files or total_chars >= max_chars_total:
                break
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            sliced = content[:max_chars_per_file]
            remaining = max_chars_total - total_chars
            if remaining <= 0:
                break
            if len(sliced) > remaining:
                sliced = sliced[:remaining]
            loaded[rel] = sliced
            total_chars += len(sliced)
        return loaded

    @staticmethod
    def _extract_diff_paths(diff_text: str) -> list[str]:
        out: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            parts = line.split(" ")
            if len(parts) < 4:
                continue
            b_path = parts[3].strip()
            if b_path.startswith("b/"):
                b_path = b_path[2:]
            if b_path and b_path != "/dev/null":
                out.append(b_path)
        return out

    @staticmethod
    def _candidate_neighbor_files(rel_path: str) -> list[str]:
        path = Path(rel_path)
        parent = path.parent
        stem = path.stem
        suffix = path.suffix
        candidates: list[str] = []
        if suffix == ".py":
            if "tests" not in path.parts:
                candidates.append((parent / "tests" / f"test_{stem}.py").as_posix())
                candidates.append((parent / f"test_{stem}.py").as_posix())
            candidates.append((parent / f"{stem}_test.py").as_posix())
        if suffix in {".js", ".ts", ".tsx"}:
            candidates.append((parent / f"{stem}.test{suffix}").as_posix())
            candidates.append((parent / f"{stem}.spec{suffix}").as_posix())
        return candidates

    def load_files(self, paths: list[str]) -> dict[str, str]:
        loaded: dict[str, str] = {}
        for raw in paths:
            path = Path(raw)
            if path.is_file():
                try:
                    loaded[str(path)] = path.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    loaded[str(path)] = ""
        return loaded

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
