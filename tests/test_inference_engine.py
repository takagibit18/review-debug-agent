"""Tests for inference engine message composition."""

from __future__ import annotations

import asyncio

from src.analyzer.context_state import ContextState
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import DebugRequest
from src.models.schemas import ModelResponse, TokenUsage
from src.tools.base import ToolResult


class FakeModelClient:
    """Record outbound model calls for assertions."""

    def __init__(self) -> None:
        self.messages = []

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def]
        self.messages = messages
        return ModelResponse(
            content='{"summary":"ok","hypotheses":[],"steps":[]}',
            tool_calls=[],
            usage=TokenUsage(total_tokens=12),
            model="fake-model",
            finish_reason="stop",
        )


def test_analyze_appends_tool_feedback_messages() -> None:
    client = FakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured debug analysis")
    request = DebugRequest(repo_path=".")
    tool_feedback = [
        {
            "tool_call": {
                "id": "call-1",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            },
            "result": ToolResult(ok=True, data={"path": "a.py", "content": "pass"}),
        }
    ]

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_feedback=tool_feedback,
        )
    )
    roles = [message.role for message in client.messages]
    assert "assistant" in roles
    assert "tool" in roles
