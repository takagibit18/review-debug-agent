"""Tests for LLM-based context part summarization."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.analyzer.context_builder import ContextPart
from src.analyzer.context_compressor import ContextCompressor
from src.models.exceptions import ModelClientError
from src.models.schemas import ModelResponse, TokenUsage


def _extract_label(prompt: str) -> str:
    match = re.search(r"^Context label:\s*(.+)$", prompt, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip()


class _FakeModelClient:
    def __init__(self, outcomes: dict[str, str | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def chat(self, messages: list[Any], config: Any = None, tools: Any = None) -> ModelResponse:  # noqa: ARG002
        await asyncio.sleep(0)
        user_prompt = str(messages[-1].content)
        label = _extract_label(user_prompt)
        self.calls.append(label)
        outcome = self.outcomes.get(label, "default summary")
        if isinstance(outcome, Exception):
            raise outcome
        return ModelResponse(
            content=outcome,
            usage=TokenUsage(total_tokens=10),
        )


def test_summarize_parts_success() -> None:
    client = _FakeModelClient(
        {
            "diff_hunk_0": "diff summary",
            "file:/a.py": "file summary",
        }
    )
    compressor = ContextCompressor(client)  # type: ignore[arg-type]
    parts = [
        ContextPart(priority=30_000, label="diff_hunk_0", content="x" * 5000),
        ContextPart(priority=40_000, label="file:/a.py", content="y" * 5000),
    ]

    summarized = asyncio.run(
        compressor.summarize_parts(parts, model_name="gpt-4o", max_summary_tokens=200)
    )

    assert len(summarized) == 2
    assert summarized[0].label == "[summarized]diff_hunk_0"
    assert summarized[0].priority == 30_000
    assert summarized[0].content.startswith("[SUMMARIZED]")
    assert summarized[1].label == "[summarized]file:/a.py"
    assert set(client.calls) == {"diff_hunk_0", "file:/a.py"}


def test_summarize_parts_degrade_on_failure() -> None:
    client = _FakeModelClient(
        {
            "error_log": ModelClientError("failed"),
            "file:/b.py": "ok summary",
        }
    )
    compressor = ContextCompressor(client)  # type: ignore[arg-type]
    parts = [
        ContextPart(priority=20_000, label="error_log", content="traceback"),
        ContextPart(priority=40_000, label="file:/b.py", content="def x(): ..."),
    ]

    summarized = asyncio.run(
        compressor.summarize_parts(parts, model_name="gpt-4o", max_summary_tokens=120)
    )

    assert len(summarized) == 1
    assert summarized[0].label == "[summarized]file:/b.py"


def test_summarize_parts_empty_inputs() -> None:
    client = _FakeModelClient({})
    compressor = ContextCompressor(client)  # type: ignore[arg-type]

    assert asyncio.run(compressor.summarize_parts([], model_name="gpt-4o")) == []
    assert (
        asyncio.run(
            compressor.summarize_parts(
                [ContextPart(priority=1, label="meta", content="{}")], model_name=""
            )
        )
        == []
    )
