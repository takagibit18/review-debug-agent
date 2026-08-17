"""Tests for inference engine message composition."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from typing import cast

from src.analyzer.context_state import ContextState
from src.analyzer.event_log import EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.trace import TraceRecorder
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.conversation import ModelConversation
from src.models.schemas import ModelResponse, TokenUsage
from src.tools.base import ToolResult


def _extract_payload_from_user_message(content: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(content.split("\n", 1)[1]))


class RecordingFakeModelClient:
    """Record model calls and emulate summary/main responses."""

    SUMMARY_SYSTEM_MARKER = "You summarize technical context for a code-analysis agent"
    REVIEW_SYSTEM_MARKER = "You are a senior code reviewer."

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []
        from src.models.schemas import ModelConfig

        self.default_config = ModelConfig(model="fake-model")
        self.configs: list[Any] = []
        self.tools: list[Any] = []
        self.policies: list[Any] = []

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        self.configs.append(config)
        self.tools.append(tools)
        self.policies.append(policy)
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


class InvalidThenValidSubmitClient(RecordingFakeModelClient):
    """Return one invalid submit_review call, then a repaired valid one."""

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        self.configs.append(config)
        self.tools.append(tools)
        if len(self.calls) == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_review",
                            "arguments": json.dumps(
                                {
                                    "summary": "found issue",
                                    "issues": [
                                        {
                                            "location": "src/x.py:10",
                                            "evidence": "x",
                                            "suggestion": "fix",
                                            "confidence": 0.9,
                                        }
                                    ],
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=11),
                model="fake-model",
                finish_reason="tool_calls",
            )
        return ModelResponse(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "repaired issue",
                                "issues": [
                                    {
                                        "severity": "warning",
                                        "location": "src/x.py:10",
                                        "evidence": "x",
                                        "suggestion": "fix",
                                        "confidence": 0.9,
                                    }
                                ],
                            }
                        ),
                    }
                }
            ],
            usage=TokenUsage(total_tokens=13),
            model="fake-model",
            finish_reason="tool_calls",
        )


class DsmlLeakThenValidSubmitClient(RecordingFakeModelClient):
    """Return one DSML-leaked submit_review call, then a repaired valid one."""

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        self.configs.append(config)
        self.tools.append(tools)
        if len(self.calls) == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_review",
                            "arguments": json.dumps(
                                {
                                    "summary": (
                                        "found issue </summary>\n"
                                        '<DSML parameter name="issues" string="false">'
                                        '[{"severity":"critical"}]'
                                    )
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=11),
                model="fake-model",
                finish_reason="tool_calls",
            )
        return ModelResponse(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "repaired issue",
                                "issues": [
                                    {
                                        "severity": "critical",
                                        "location": "src/x.py:10",
                                        "evidence": "+ if modified_val:",
                                        "suggestion": "Preserve the modified value.",
                                        "confidence": 0.95,
                                    }
                                ],
                            }
                        ),
                    }
                }
            ],
            usage=TokenUsage(total_tokens=13),
            model="fake-model",
            finish_reason="tool_calls",
        )


class IssueLikeSummaryThenValidSubmitClient(RecordingFakeModelClient):
    """Return empty issues with issue-like summary language, then a repaired valid issue."""

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        self.configs.append(config)
        self.tools.append(tools)
        if len(self.calls) == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_review",
                            "arguments": json.dumps(
                                {
                                    "summary": (
                                        "The change is a behavioral modification. "
                                        "One concern: the callable-generated ID path can "
                                        "silently replace stable long IDs."
                                    ),
                                    "issues": [],
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=11),
                model="fake-model",
                finish_reason="tool_calls",
            )
        return ModelResponse(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {
                                "summary": "repaired issue",
                                "issues": [
                                    {
                                        "severity": "warning",
                                        "location": "src/_pytest/python.py:1200",
                                        "evidence": (
                                            "+    if modified_val is None or "
                                            "len(modified_val) > 100:\n"
                                            "+        return str(argname) + str(idx)"
                                        ),
                                        "suggestion": (
                                            "Preserve callable-generated IDs or document "
                                            "the compatibility change."
                                        ),
                                        "confidence": 0.85,
                                    }
                                ],
                            }
                        ),
                    }
                }
            ],
            usage=TokenUsage(total_tokens=13),
            model="fake-model",
            finish_reason="tool_calls",
        )


def test_analyze_does_not_rebuild_provider_turns_from_business_feedback(
    monkeypatch,
) -> None:
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
    assert "assistant" not in roles
    assert "tool" not in roles


def test_analyze_injects_synthetic_prefetch_feedback_as_user_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")
    tool_feedback = [
        {
            "tool_call": {
                "id": "prefetch-read-file-0",
                "type": "function",
                "synthetic_context": True,
                "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
            },
            "result": ToolResult(
                ok=True, data={"file_path": "a.py", "content": "1: pass"}
            ),
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
    assert "tool" not in roles
    assert "assistant" not in roles
    assert any(
        message.role == "user" and "prefetched_tool_context" in message.content
        for message in client.calls[-1]
    )


def test_analyze_suppresses_prefetch_covered_by_selected_file_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    loaded = "".join(f"line {number}\n" for number in range(1, 101))
    tool_feedback = [
        {
            "tool_call": {
                "id": "prefetch-read-file-0",
                "type": "function",
                "synthetic_context": True,
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"src/app.py"}',
                },
            },
            "result": ToolResult(
                ok=True,
                data={
                    "file_path": "src/app.py",
                    "content": "41: line 41\n42: line 42",
                    "start_line": 41,
                    "line_count": 2,
                },
            ),
        }
    ]

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            file_contents={"src/app.py": loaded},
            tool_feedback=tool_feedback,
        )
    )

    assert not any(
        "prefetched_tool_context" in message.content for message in client.calls[-1]
    )


def test_analyze_keeps_prefetch_when_loaded_file_is_not_selected(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    loaded = "".join(f"line {number}\n" for number in range(1, 101))
    tool_feedback = [
        {
            "tool_call": {
                "id": "prefetch-read-file-0",
                "type": "function",
                "synthetic_context": True,
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"src/app.py"}',
                },
            },
            "result": ToolResult(
                ok=True,
                data={
                    "file_path": "src/app.py",
                    "content": "41: line 41\n42: line 42",
                    "start_line": 41,
                    "line_count": 2,
                },
            ),
        }
    ]

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            file_contents={"src/app.py": loaded},
            tool_feedback=tool_feedback,
            prompt_input_token_budget=1,
        )
    )

    assert any(
        "prefetched_tool_context" in message.content for message in client.calls[-1]
    )


def test_prefetch_coverage_requires_selected_file_to_reach_end_line() -> None:
    raw_call = {
        "synthetic_context": True,
        "function": {
            "name": "read_file",
            "arguments": '{"file_path":"src/app.py"}',
        },
    }
    result = {
        "ok": True,
        "data": {
            "start_line": 41,
            "line_count": 2,
            "content": "41: line 41\n42: line 42",
        },
    }

    assert not InferenceEngine._prefetch_covered_by_selected_file(  # noqa: SLF001
        raw_call,
        result,
        {"src/app.py": 20},
    )


def test_synthetic_prefetch_feedback_is_compacted(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")
    oversized_content = "\n".join(f"{idx}: line" for idx in range(1000))
    tool_feedback = [
        {
            "tool_call": {
                "id": "prefetch-read-file-0",
                "type": "function",
                "synthetic_context": True,
                "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
            },
            "result": ToolResult(
                ok=True,
                data={
                    "file_path": "a.py",
                    "content": oversized_content,
                    "start_line": 1,
                    "line_count": 1000,
                    "truncated": False,
                },
            ),
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

    prefetch_messages = [
        message.content
        for message in client.calls[-1]
        if message.role == "user" and "prefetched_tool_context" in message.content
    ]
    assert len(prefetch_messages) == 1
    assert len(prefetch_messages[0]) < 5000
    assert "truncated_for_prompt" in prefetch_messages[0]


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
        payload
        for event_type, _, payload in events
        if event_type == EventType.MODEL_RESPONSE_DETAIL
    )
    assert model_event["iteration"] == 1
    assert "content_length" in model_event
    assert "reasoning_content_length" not in model_event
    assert "tool_choice" in model_event
    assert "thinking_disabled" in model_event
    plan_event = next(
        payload
        for event_type, _, payload in events
        if event_type == EventType.PLAN_PARSED
    )
    assert plan_event["iteration"] == 1


def test_analyze_logs_length_finish_reason_even_without_trace_detail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()

    async def _length_response(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        return ModelResponse(
            content="",
            tool_calls=[],
            usage=TokenUsage(
                prompt_tokens=100, completion_tokens=2048, total_tokens=2148
            ),
            model="fake-model",
            finish_reason="length",
            reasoning_content="x" * 1000,
        )

    client.chat = _length_response  # type: ignore[method-assign]
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_recorder=TraceRecorder(
            detail_mode="off", max_chars=500, log_tool_body=False
        ),
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            iteration=2,
        )
    )

    length_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.ERROR and phase == "analyze"
    ]
    assert length_events
    length_event = next(
        event
        for event in length_events
        if event["reason"] == "model_finish_reason_length"
    )
    assert length_event["iteration"] == 2


def test_analyze_marks_length_finish_without_output_incomplete(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()

    async def _length_response(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        return ModelResponse(
            content="",
            tool_calls=[],
            usage=TokenUsage(
                prompt_tokens=100, completion_tokens=2048, total_tokens=2148
            ),
            model="deepseek-v4-pro",
            finish_reason="length",
            reasoning_content="x" * 1000,
        )

    client.chat = _length_response  # type: ignore[method-assign]
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )

    plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            iteration=2,
        )
    )

    assert plan.incomplete_reason == "model_finish_reason_length_no_submit"
    assert plan.recovery_required is True
    incomplete_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.ERROR
        and phase == "analyze"
        and payload.get("reason") == "model_finish_reason_length_no_submit"
    ]
    assert incomplete_events


def test_analyze_marks_length_blank_review_submit_incomplete(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()

    async def _length_blank_submit(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        return ModelResponse(
            tool_calls=[
                {
                    "id": "blank-submit",
                    "function": {
                        "name": "submit_review",
                        "arguments": '{"summary":"","issues":[]}',
                    },
                }
            ],
            usage=TokenUsage(total_tokens=4096),
            model="deepseek-v4-pro",
            finish_reason="length",
        )

    client.chat = _length_blank_submit  # type: ignore[method-assign]
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.summary == ""
    assert plan.draft_review.issues == []
    assert plan.incomplete_reason == "model_finish_reason_length_no_submit"
    assert plan.recovery_required is True


def test_analyze_accepts_length_explicit_empty_review_submit(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()

    async def _length_explicit_submit(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        return ModelResponse(
            tool_calls=[
                {
                    "id": "explicit-empty-submit",
                    "function": {
                        "name": "submit_review",
                        "arguments": (
                            '{"summary":"No supported issues found.","issues":[]}'
                        ),
                    },
                }
            ],
            usage=TokenUsage(total_tokens=4096),
            model="deepseek-v4-pro",
            finish_reason="length",
        )

    client.chat = _length_explicit_submit  # type: ignore[method-assign]
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.summary == "No supported issues found."
    assert plan.draft_review.issues == []
    assert plan.incomplete_reason == ""
    assert plan.recovery_required is False


def test_regular_review_enables_high_thinking_for_exploration(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    client.default_config = client.default_config.model_copy(
        update={"model": "deepseek-v4-pro"}
    )
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            iteration=0,
            near_last_iteration=False,
            force_submit=False,
        )
    )

    assert client.policies[-1].thinking == "high"
    assert client.policies[-1].forced_tool is None
    assert client.configs[-1].max_tokens == 12288


def test_analyze_records_context_telemetry_without_content(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            diff_text="diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            file_contents={"src/app.py": "print('new')\n"},
            prompt_input_token_budget=4096,
        )
    )

    telemetry = next(
        payload
        for event_type, phase, payload in events
        if event_type == EventType.CONTEXT_TELEMETRY and phase == "analyze"
    )
    assert telemetry["tool_schema_count"] == 1
    assert telemetry["message_count_by_role"] == {
        "system": 1,
        "user": 1,
        "assistant": 0,
        "tool": 0,
    }
    assert telemetry["message_shapes"][0]["component"] == "system"
    assert telemetry["message_shapes"][1]["component"] == "review_payload"
    assert telemetry["tool_schema_shapes"][0]["name"] == "submit_review"
    assert telemetry["tool_schema_chars"] > 0
    assert telemetry["assembled_request_chars"] > telemetry["message_chars"]
    assert telemetry["max_output_tokens"] == 12288
    assert telemetry["thinking"] == "high"
    assert telemetry["selected"]["tokens"] > 0
    assert telemetry["selected"]["by_kind"]["diff_hunk"]["chars"] > 0
    assert "print('new')" not in json.dumps(telemetry)


def test_context_telemetry_records_prefetch_coverage_without_source(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )
    loaded = "".join(f"line {number}\n" for number in range(1, 101))
    tool_feedback = [
        {
            "tool_call": {
                "id": "prefetch-read-file-0",
                "type": "function",
                "synthetic_context": True,
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"src/app.py"}',
                },
            },
            "result": ToolResult(
                ok=True,
                data={
                    "file_path": "src/app.py",
                    "content": "41: line 41\n42: line 42",
                    "start_line": 41,
                    "line_count": 2,
                },
            ),
        }
    ]

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            file_contents={"src/app.py": loaded},
            tool_feedback=tool_feedback,
        )
    )

    telemetry = next(
        payload
        for event_type, phase, payload in events
        if event_type == EventType.CONTEXT_TELEMETRY and phase == "analyze"
    )
    coverage = telemetry["prefetch_coverage"]
    assert coverage["entry_count"] == 1
    assert coverage["covered_entry_count"] == 1
    assert coverage["suppressed_entry_count"] == 1
    assert coverage["entries"][0] == {
        "file": "src/app.py",
        "start_line": 41,
        "end_line": 42,
        "prefetch_content_chars": 23,
        "loaded_file_chars": len(loaded),
        "loaded_complete_lines": 100,
        "covered_by_file_context": True,
    }
    assert "line 41" not in json.dumps(telemetry)


def test_normalize_review_payload_canonicalizes_location() -> None:
    payload = {
        "summary": "ok",
        "issues": [
            {
                "severity": "warning",
                "location": "in src\\api\\handler.py:33",
                "evidence": "x",
                "suggestion": "y",
                "confidence": 0.6,
            }
        ],
    }
    normalized, warnings = InferenceEngine._normalize_review_payload(payload)
    issues = normalized["issues"]
    assert issues[0]["location"] == "src/api/handler.py:33"
    assert warnings


def test_force_submit_review_forces_submit_tool_and_disables_deepseek_thinking(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    client.default_config = client.default_config.model_copy(
        update={"model": "deepseek-v4-pro"}
    )
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            force_submit=True,
        )
    )

    config = client.configs[-1]
    assert config.max_tokens == 4096
    assert client.policies[-1].forced_tool == "submit_review"
    assert client.policies[-1].thinking == "off"
    assert any(
        "directly contain top-level summary and issues" in message.content
        for message in client.calls[-1]
    )
    assert [tool["function"]["name"] for tool in client.tools[-1]] == ["submit_review"]
    assert any(
        "summary must not mention bugs, regressions" in str(message.content)
        for message in client.calls[-1]
    )


def test_force_submit_places_replayed_tool_history_before_evidence_and_final_notice(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    conversation = ModelConversation()
    read_call = {
        "id": "prior-read",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path":"pkg/wrapper.py"}',
        },
    }
    conversation.add_assistant_tool_turn(
        response_id="prior-response",
        content="",
        thinking="provider replay only",
        tool_calls=[read_call],
    )
    conversation.add_tool_result(
        "prior-read",
        {"ok": True, "content": "return self.obj == other"},
    )
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        conversation=conversation,
    )

    asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            tool_feedback=[
                {
                    "iteration": 0,
                    "tool_call": read_call,
                    "result": ToolResult(
                        ok=True,
                        data={
                            "file_path": "pkg/wrapper.py",
                            "content": "EVIDENCE-MARKER: return self.obj == other",
                        },
                    ),
                }
            ],
            force_submit=True,
        )
    )

    messages = client.calls[-1]
    assistant_index = next(
        index for index, message in enumerate(messages) if message.role == "assistant"
    )
    tool_index = next(
        index
        for index, message in enumerate(messages)
        if message.role == "tool" and message.tool_call_id == "prior-read"
    )
    evidence_index = next(
        index
        for index, message in enumerate(messages)
        if "final_submit_evidence_summary" in message.content
    )
    notice_index = next(
        index
        for index, message in enumerate(messages)
        if "FINAL CALL" in message.content
    )

    assert assistant_index < tool_index < evidence_index < notice_index
    assert notice_index == len(messages) - 1
    assert messages[assistant_index].thinking == "provider replay only"


def test_force_submit_review_uses_compact_reserved_prompt_budget(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("FINAL_SUBMIT_PROMPT_TOKEN_BUDGET", "100")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(
        goal="Run structured code review",
        candidate_context_manifests=[
            {
                "candidate_id": "C-large",
                "included_spans": [{"content": "GRAPH-MARKER" * 5000}],
                "included_graph_paths": [],
            }
        ],
    )

    asyncio.run(
        engine.analyze(
            state=state,
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            diff_text="DIFF-MARKER" * 5000,
            file_contents={"large.py": "FILE-MARKER" * 5000},
            prompt_input_token_budget=10_000,
            force_submit=True,
        )
    )

    user_payload = _extract_payload_from_user_message(client.calls[-1][1].content)
    assert user_payload["candidate_context_manifests"] == []
    assert "GRAPH-MARKER" not in client.calls[-1][1].content
    assert "FILE-MARKER" not in client.calls[-1][1].content
    assert "DIFF-MARKER" not in client.calls[-1][1].content


def test_length_tool_result_is_retained_in_bounded_force_submit_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("FINAL_SUBMIT_PROMPT_TOKEN_BUDGET", "4000")
    monkeypatch.setenv("FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET", "1200")
    client = RecordingFakeModelClient()
    call_count = 0

    async def _sequenced_response(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        if call_count == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "id": "read-after-length",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"file_path":"pkg/wrapper.py"}',
                        },
                    }
                ],
                usage=TokenUsage(total_tokens=2048),
                model="deepseek-v4-pro",
                finish_reason="length",
                reasoning_content=(
                    "Compatibility regression: wrapper equality now compares the "
                    "wrapped object against the wrapper instead of other.obj."
                ),
            )
        return ModelResponse(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "submit_review",
                        "arguments": '{"summary":"review","issues":[]}',
                    }
                }
            ],
            usage=TokenUsage(total_tokens=20),
            model="deepseek-v4-pro",
            finish_reason="tool_calls",
        )

    client.chat = _sequenced_response  # type: ignore[method-assign]
    events: list[tuple[EventType, str, dict[str, Any]]] = []
    engine = InferenceEngine(
        model_client=client,  # type: ignore[arg-type]
        trace_event_writer=lambda event_type, phase, payload: events.append(
            (event_type, phase, payload)
        ),
    )
    request = ReviewRequest(repo_path=".")
    first_plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=request,
            tool_specs=[],
            iteration=1,
            near_last_iteration=False,
        )
    )
    assert first_plan.model_finish_reason == "length"
    assert first_plan.incomplete_reason == "model_finish_reason_length_no_submit"
    assert first_plan.recovery_required is True
    assert len(first_plan.tool_calls) == 1

    feedback = [
        {
            "iteration": 1,
            "tool_call": first_plan.tool_calls[0],
            "result": ToolResult(ok=True, data={"content": "duplicate"}),
        },
        {
            "iteration": 1,
            "tool_call": first_plan.tool_calls[0],
            "result": ToolResult(
                ok=True,
                data={
                    "file_path": "pkg/wrapper.py",
                    "content": "EVIDENCE-MARKER: return self.obj == other",
                },
            ),
        },
    ]
    final_plan, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            tool_feedback=feedback,
            iteration=1,
            force_submit=True,
        )
    )

    final_digest = next(
        message.content
        for message in client.calls[-1]
        if "final_submit_evidence_summary" in message.content
    )
    assert "EVIDENCE-MARKER" in final_digest
    assert final_digest.count("tool_evidence") == 1
    assert final_plan.final_submit_evidence_included_count == 1
    assert 0 < final_plan.final_submit_evidence_token_count <= 1200
    telemetry = next(
        payload
        for event_type, phase, payload in reversed(events)
        if event_type == EventType.CONTEXT_TELEMETRY
        and phase == "analyze"
        and payload["force_submit"] is True
    )
    assert telemetry["prompt_input_token_budget"] == 4000
    assert telemetry["base_context_token_budget"] == 2800
    assert telemetry["final_submit_feedback_token_budget"] == 1200
    assert telemetry["final_submit_evidence"]["deduplicated_count"] == 1


def test_force_submit_review_disables_qwen_dashscope_thinking(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv(
        "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    client = RecordingFakeModelClient()
    client.default_config = client.default_config.model_copy(
        update={"model": "qwen3.6-27b"}
    )
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            force_submit=True,
        )
    )

    assert client.policies[-1].forced_tool == "submit_review"
    assert client.policies[-1].thinking == "off"


def test_force_submit_review_disables_glm_dashscope_thinking(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv(
        "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    client = RecordingFakeModelClient()
    client.default_config = client.default_config.model_copy(
        update={"model": "glm-4.7"}
    )
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
            force_submit=True,
        )
    )

    assert client.policies[-1].forced_tool == "submit_review"
    assert client.policies[-1].thinking == "off"


def test_force_submit_debug_forces_debug_tool_without_openai_thinking_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    client = RecordingFakeModelClient()
    client.default_config = client.default_config.model_copy(update={"model": "gpt-4o"})
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured debug analysis")
    request = DebugRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_debug"}}],
            force_submit=True,
        )
    )

    config = client.configs[-1]
    assert client.policies[-1].forced_tool == "submit_debug"
    assert config.extra_body is None


def test_near_last_review_iteration_switches_to_submit_only_forced_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "submit_review"}},
                {"type": "function", "function": {"name": "submit_debug"}},
            ],
            near_last_iteration=True,
        )
    )

    config = client.configs[-1]
    assert config.max_tokens == 4096
    assert client.policies[-1].forced_tool == "submit_review"
    assert [tool["function"]["name"] for tool in client.tools[-1]] == ["submit_review"]


def test_invalid_submit_review_arguments_do_not_create_empty_draft() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": '{"summary": "truncated", "issues": [',
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert plan.draft_review is None
    assert "Invalid JSON" in parse_meta["submit_review_validation_error"]


def test_nested_submit_review_arguments_are_strictly_normalized() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    nested = {
        "arguments": {
            "summary": "Found one supported issue.",
            "issues": [
                {
                    "severity": "warning",
                    "location": "src/a.py:1",
                    "evidence": "The changed branch returns the wrong value.",
                    "suggestion": "Return the preserved value.",
                    "confidence": 0.9,
                }
            ],
        }
    }

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps(nested),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert parse_meta["submit_review_arguments_normalized"] is True
    assert parse_meta["submit_review_validation_error"] == ""
    assert plan.draft_review is not None
    assert len(plan.draft_review.issues) == 1


def test_unrelated_arguments_envelope_is_not_normalized() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps(
                        {"arguments": {"summary": "Missing issues."}}
                    ),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert parse_meta["submit_review_arguments_normalized"] is False
    assert plan.draft_review is None
    assert "issues" in parse_meta["submit_review_validation_error"]


def test_submit_review_arguments_allow_unescaped_control_characters() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    arguments = (
        '{"summary": "ok", "issues": [{"severity": "warning", '
        '"location": "src/a.py:1", '
        '"evidence": "if value:\n    return value", '
        '"suggestion": "Keep the branch safe.", "confidence": 0.9}]}'
    )
    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [{"function": {"name": "submit_review", "arguments": arguments}}],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert parse_meta["submit_review_validation_error"] == ""
    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].evidence == "if value:\n    return value"


def test_submit_review_payload_requires_explicit_issues_list() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps({"summary": "looks clean"}),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert plan.draft_review is None
    assert "issues" in parse_meta["submit_review_validation_error"]


def test_submit_review_rejects_issue_missing_confidence() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps(
                        {
                            "summary": "found one issue",
                            "issues": [
                                {
                                    "severity": "warning",
                                    "location": "src/x.py:10",
                                    "evidence": "+ changed",
                                    "suggestion": "preserve the value",
                                }
                            ],
                        }
                    ),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert plan.draft_review is None
    assert "missing required confidence" in parse_meta["submit_review_validation_error"]


def test_fallback_review_json_cannot_bypass_missing_confidence() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    request = ReviewRequest(repo_path=".")

    missing = {
        "summary": "found one issue",
        "issues": [
            {
                "severity": "warning",
                "location": "src/x.py:10",
                "evidence": "+ changed",
                "suggestion": "preserve the value",
            }
        ],
    }
    assert engine._try_parse_submit_payload_from_json(missing, request) is None  # noqa: SLF001

    present = {
        "summary": "found one issue",
        "issues": [
            {
                "severity": "warning",
                "location": "src/x.py:10",
                "evidence": "+ changed",
                "suggestion": "preserve the value",
                "confidence": 0.9,
            }
        ],
    }
    parsed = engine._try_parse_submit_payload_from_json(present, request)  # noqa: SLF001
    assert parsed is not None
    assert parsed.draft_review is not None
    assert parsed.draft_review.issues[0].confidence == 0.9


def test_submit_review_rejects_dsml_issues_parameter_leak_in_summary() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    leaked_summary = (
        'review notes </summary>\n<DSML parameter name="issues" string="false">[]'
    )

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps({"summary": leaked_summary, "issues": []}),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert plan.draft_review is None
    assert "DSML" in parse_meta["submit_review_validation_error"]


def test_submit_review_rejects_issue_like_summary_with_empty_issues() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps(
                        {
                            "summary": (
                                "This is a behavioral modification. One concern: "
                                "callable IDs may be replaced."
                            ),
                            "issues": [],
                        }
                    ),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert plan.draft_review is None
    assert (
        "summary mentions review concerns"
        in parse_meta["submit_review_validation_error"]
    )


def test_submit_review_allows_empty_issues_with_honest_no_bug_summary() -> None:
    client = RecordingFakeModelClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, parse_meta = engine._parse_tool_calls(  # noqa: SLF001
        [
            {
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps(
                        {
                            "summary": (
                                "No concrete bugs, regressions, or breaking changes "
                                "are evident from the diff alone."
                            ),
                            "issues": [],
                        }
                    ),
                }
            }
        ],
        ReviewRequest(repo_path="."),
        force_submit=True,
    )

    assert parse_meta["submit_review_validation_error"] == ""
    assert plan.draft_review is not None
    assert plan.draft_review.issues == []


def test_invalid_submit_review_payload_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = InvalidThenValidSubmitClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, usage = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "warning"
    assert usage.total_tokens == 24
    assert len(client.calls) == 2
    assert any("issues.0.severity" in message.content for message in client.calls[1])
    assert any(
        "directly contain top-level summary and issues" in message.content
        for message in client.calls[1]
    )


def test_missing_confidence_submit_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")

    class MissingConfidenceThenValidClient(RecordingFakeModelClient):
        async def chat(  # type: ignore[override]
            self, messages, config=None, tools=None, policy=None, conversation=None
        ):
            self.calls.append(messages)
            self.configs.append(config)
            self.tools.append(tools)
            if len(self.calls) == 1:
                return ModelResponse(
                    content="",
                    tool_calls=[
                        {
                            "function": {
                                "name": "submit_review",
                                "arguments": json.dumps(
                                    {
                                        "summary": "found issue",
                                        "issues": [
                                            {
                                                "severity": "warning",
                                                "location": "src/x.py:10",
                                                "evidence": "+ changed",
                                                "suggestion": "preserve the value",
                                            }
                                        ],
                                    }
                                ),
                            }
                        }
                    ],
                    usage=TokenUsage(total_tokens=11),
                    model="fake-model",
                    finish_reason="tool_calls",
                )
            return ModelResponse(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "submit_review",
                            "arguments": json.dumps(
                                {
                                    "summary": "found issue",
                                    "issues": [
                                        {
                                            "severity": "warning",
                                            "location": "src/x.py:10",
                                            "evidence": "+ changed",
                                            "suggestion": "preserve the value",
                                            "confidence": 0.9,
                                        }
                                    ],
                                }
                            ),
                        }
                    }
                ],
                usage=TokenUsage(total_tokens=13),
                model="fake-model",
                finish_reason="tool_calls",
            )

    client = MissingConfidenceThenValidClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, usage = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert len(client.calls) == 2
    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].confidence == 0.9
    assert plan.draft_review.issues[0].location == "src/x.py:10"
    assert plan.draft_review.issues[0].evidence == "+ changed"


def test_length_invalid_submit_defers_to_length_recovery_without_schema_repair(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = RecordingFakeModelClient()

    async def _length_invalid_submit(
        messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        client.calls.append(messages)
        client.configs.append(config)
        client.tools.append(tools)
        return ModelResponse(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "submit_review",
                        "arguments": '{"summary":"truncated","issues":[',
                    }
                }
            ],
            usage=TokenUsage(total_tokens=2048),
            model="deepseek-v4-flash",
            finish_reason="length",
        )

    client.chat = _length_invalid_submit  # type: ignore[method-assign]
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]

    plan, usage = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            tool_schemas=[
                {"type": "function", "function": {"name": "submit_review"}}
            ],
            force_submit=True,
        )
    )

    assert len(client.calls) == 1
    assert usage.total_tokens == 2048
    assert plan.draft_review is None
    assert plan.incomplete_reason == "model_finish_reason_length_no_submit"
    assert plan.recovery_required is True


def test_dsml_leaked_submit_review_payload_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = DsmlLeakThenValidSubmitClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, usage = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "critical"
    assert usage.total_tokens == 24
    assert len(client.calls) == 2
    assert any("DSML parameter leak" in message.content for message in client.calls[1])


def test_issue_like_empty_submit_review_payload_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = IssueLikeSummaryThenValidSubmitClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, usage = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "warning"
    assert usage.total_tokens == 24
    assert len(client.calls) == 2
    assert any(
        "summary mentions review concerns" in message.content
        for message in client.calls[1]
    )
