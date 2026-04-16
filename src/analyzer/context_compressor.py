"""LLM-based context compression for overflowed context parts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.analyzer.context_builder import ContextPart
from src.models.client import ModelClient
from src.models.exceptions import ModelClientError
from src.models.schemas import Message, ModelConfig

_SUMMARY_LABEL_PREFIX = "[summarized]"
_SUMMARY_CONTENT_PREFIX = "[SUMMARIZED]\n"


@dataclass(frozen=True)
class SummaryRequest:
    """Normalized request for one context part summary."""

    source_label: str
    source_content: str


class ContextCompressor:
    """Compress oversized context parts by calling the same model."""

    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    async def summarize_parts(
        self,
        parts: list[ContextPart],
        *,
        model_name: str,
        max_summary_tokens: int = 1000,
    ) -> list[ContextPart]:
        """Summarize multiple context parts; failed parts are silently skipped."""
        if not parts:
            return []
        if not model_name.strip():
            return []

        jobs = [
            self._summarize_one(
                SummaryRequest(source_label=part.label, source_content=part.content),
                source_priority=part.priority,
                model_name=model_name,
                max_summary_tokens=max_summary_tokens,
            )
            for part in parts
            if part.content.strip()
        ]
        if not jobs:
            return []

        compressed = await asyncio.gather(*jobs)
        return [item for item in compressed if item is not None]

    async def _summarize_one(
        self,
        request: SummaryRequest,
        *,
        source_priority: int,
        model_name: str,
        max_summary_tokens: int,
    ) -> ContextPart | None:
        prompt = self._build_summary_prompt(request.source_label, request.source_content)
        try:
            response = await self._model_client.chat(
                messages=[
                    Message(
                        role="system",
                        content=(
                            "You summarize technical context for a code-analysis agent. "
                            "Keep only facts useful for debugging/review and avoid speculation."
                        ),
                    ),
                    Message(role="user", content=prompt),
                ],
                config=ModelConfig(
                    model=model_name,
                    temperature=0.0,
                    max_tokens=max_summary_tokens,
                ),
            )
        except ModelClientError:
            return None

        summary_text = (response.content or "").strip()
        if not summary_text:
            return None

        return ContextPart(
            priority=source_priority,
            label=f"{_SUMMARY_LABEL_PREFIX}{request.source_label}",
            content=f"{_SUMMARY_CONTENT_PREFIX}{summary_text}",
            token_count=0,
        )

    @staticmethod
    def _build_summary_prompt(label: str, content: str) -> str:
        kind = ContextCompressor._label_kind(label)
        guidance = {
            "diff": (
                "Summarize changed files, key hunks, behavioral impact, and potential risk. "
                "Preserve file paths and function names when visible."
            ),
            "file": (
                "Summarize architecture-relevant symbols (classes/functions), major logic branches, "
                "and invariants related to bug/risk analysis."
            ),
            "error_log": (
                "Summarize exception type, stack traces, failing modules, and reproducible signals. "
                "Keep timestamps/error codes if present."
            ),
            "structure": (
                "Summarize project layout, important directories, and entry points relevant to analysis."
            ),
            "other": (
                "Summarize the most decision-relevant technical facts for follow-up code analysis."
            ),
        }[kind]
        return (
            f"Context label: {label}\n"
            f"Summary target: {kind}\n"
            "Rules:\n"
            "- Keep under 12 bullet points.\n"
            "- Keep critical literals (paths, symbols, error types).\n"
            "- Do not include markdown code fences.\n"
            "- If content is mostly noise, say so briefly.\n\n"
            f"Task guidance: {guidance}\n\n"
            "Content:\n"
            f"{content}"
        )

    @staticmethod
    def _label_kind(label: str) -> str:
        if label.startswith("diff_hunk_"):
            return "diff"
        if label.startswith("file:"):
            return "file"
        if label == "error_log":
            return "error_log"
        if label == "structure":
            return "structure"
        return "other"
