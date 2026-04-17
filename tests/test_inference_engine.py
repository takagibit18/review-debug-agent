"""Tests for inference engine message composition."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.event_log import EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.trace import TraceRecorder
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import ModelResponse, TokenUsage
from src.tools.base import ToolResult


def _extract_payload_from_user_message(content: str) -> dict[str, Any]:
    return json.loads(content.split("\n", 1)[1])


class RecordingFakeModelClient:
    """Record model calls and emulate summary/main responses."""

    SUMMARY_SYSTEM_MARKER = "You summarize technical context for a code-analysis agent"
    REVIEW_SYSTEM_MARKER = "You are a senior code reviewer."

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        first_content = str(messages[0].content)
        if self.SUMMARY_SYSTEM_MARKER in first_content:
            return ModelResponse(
                content="- summarized key facts",
                tool_calls=[],
                usage=TokenUsage(total_tokens=7),
                model="fake-model",
                finish_reason="stop",
            )
        if self.REVIEW_SYSTEM_MARKER in first_content:
            return ModelResponse(
                content='{"summary":"review ok","issues":[]}',
                tool_calls=[],
                usage=TokenUsage(total_tokens=12),
                model="fake-model",
                finish_reason="stop",
            )
        return ModelResponse(
            content='{"summary":"debug ok","hypotheses":[],"steps":[]}',
            tool_calls=[],
            usage=TokenUsage(total_tokens=12),
            model="fake-model",
            finish_reason="stop",
        )

    def summary_call_count(self) -> int:
        return sum(
            1
            for call in self.calls
            if self.SUMMARY_SYSTEM_MARKER in str(call[0].content)
        )


def test_analyze_appends_tool_feedback_messages(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
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
    roles = [message.role for message in client.calls[-1]]
    assert "assistant" in roles
    assert "tool" in roles


def test_analyze_summary_disabled_only_one_main_call(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured debug analysis")
    request = DebugRequest(repo_path=".", error_log_text="short error")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            prompt_input_token_budget=5000,
        )
    )

    assert len(client.calls) == 1
    assert client.summary_call_count() == 0
    user_payload = _extract_payload_from_user_message(client.calls[0][1].content)
    assert user_payload["truncated"].get("summarized", []) == []


def test_analyze_review_overflow_uses_summary(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "true")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    diff_text = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,3 @@\n"
        + ("+x\n" * 5000)
    )
    request = ReviewRequest(repo_path=".", diff_text=diff_text)

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            diff_text=diff_text,
            prompt_input_token_budget=80,
        )
    )

    assert len(client.calls) >= 2
    assert client.summary_call_count() >= 1
    final_user_payload = _extract_payload_from_user_message(client.calls[-1][1].content)
    assert final_user_payload["truncated"]["summarized"]
    assert "[SUMMARIZED]" in final_user_payload["diff_loaded"]


def test_analyze_debug_overflow_uses_summary(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "true")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured debug analysis")
    error_log = "Traceback\n" + ("ValueError: boom\n" * 4000)
    request = DebugRequest(repo_path=".", error_log_text=error_log)

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            error_log=error_log,
            prompt_input_token_budget=80,
        )
    )

    assert len(client.calls) >= 2
    assert client.summary_call_count() >= 1
    final_user_payload = _extract_payload_from_user_message(client.calls[-1][1].content)
    assert final_user_payload["truncated"]["summarized"]
    assert final_user_payload["error_log_loaded"].startswith("[SUMMARIZED]")


def test_analyze_emits_model_detail_and_plan_parsed_events(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    trace = TraceRecorder(detail_mode="compact", max_chars=500, log_tool_body=False)
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_recorder=trace,
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            iteration=1,
        )
    )

    event_types = [event_type for event_type, _, _ in events]
    assert EventType.MODEL_RESPONSE_DETAIL in event_types
    assert EventType.PLAN_PARSED in event_types
    model_event = next(
        payload for event_type, _, payload in events if event_type == EventType.MODEL_RESPONSE_DETAIL
    )
    assert model_event["iteration"] == 1
    plan_event = next(
        payload for event_type, _, payload in events if event_type == EventType.PLAN_PARSED
    )
    assert plan_event["iteration"] == 1
