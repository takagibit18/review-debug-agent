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
                ["git", "-C", repo_path, "diff", "--cached"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return ""
            return result.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

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
