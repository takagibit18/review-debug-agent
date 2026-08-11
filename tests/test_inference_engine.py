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

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def,unused-argument]
        self.calls.append(messages)
        self.configs.append(config)
        self.tools.append(tools)
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

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def,unused-argument]
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

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def,unused-argument]
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

    async def chat(self, messages, config=None, tools=None):  # type: ignore[no-untyped-def,unused-argument]
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
    assert "reasoning_content_length" in model_event
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

    async def _length_response(messages, config=None, tools=None):  # type: ignore[no-untyped-def]
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

    async def _length_response(messages, config=None, tools=None):  # type: ignore[no-untyped-def]
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

    plan, _, _ = asyncio.run(
        engine.analyze(
            state=ContextState(goal="Run structured code review"),
            request=ReviewRequest(repo_path="."),
            tool_specs=[],
            iteration=2,
        )
    )

    assert plan.incomplete_reason == "model_finish_reason_length_no_output"
    incomplete_events = [
        payload
        for event_type, phase, payload in events
        if event_type == EventType.ERROR
        and phase == "analyze"
        and payload.get("reason") == "model_finish_reason_length_no_output"
    ]
    assert incomplete_events


def test_regular_review_disables_deepseek_thinking(monkeypatch) -> None:
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

    config = client.configs[-1]
    assert config.extra_body == {"thinking": {"type": "disabled"}}


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
    assert telemetry["selected"]["tokens"] > 0
    assert telemetry["selected"]["by_kind"]["diff_hunk"]["chars"] > 0
    assert "print('new')" not in json.dumps(telemetry)


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
    assert config.max_tokens == 2048
    assert config.tool_choice == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
    assert config.extra_body == {"thinking": {"type": "disabled"}}
    assert [tool["function"]["name"] for tool in client.tools[-1]] == ["submit_review"]
    assert any(
        "summary must not mention bugs, regressions" in str(message.content)
        for message in client.calls[-1]
    )


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

    config = client.configs[-1]
    assert config.tool_choice == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
    assert config.extra_body == {"enable_thinking": False}


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

    config = client.configs[-1]
    assert config.tool_choice == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
    assert config.extra_body == {"enable_thinking": False}


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
    assert config.tool_choice == {
        "type": "function",
        "function": {"name": "submit_debug"},
    }
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
    assert config.max_tokens == 2048
    assert config.tool_choice == {
        "type": "function",
        "function": {"name": "submit_review"},
    }
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

    plan, total_tokens, _ = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "warning"
    assert total_tokens == 24
    assert len(client.calls) == 2
    assert any("issues.0.severity" in message.content for message in client.calls[1])


def test_dsml_leaked_submit_review_payload_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = DsmlLeakThenValidSubmitClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, total_tokens, _ = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "critical"
    assert total_tokens == 24
    assert len(client.calls) == 2
    assert any("DSML parameter leak" in message.content for message in client.calls[1])


def test_issue_like_empty_submit_review_payload_gets_repair_retry(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = IssueLikeSummaryThenValidSubmitClient()
    engine = InferenceEngine(model_client=client)  # type: ignore[arg-type]
    state = ContextState(goal="Run structured code review")
    request = ReviewRequest(repo_path=".")

    plan, total_tokens, _ = asyncio.run(
        engine.analyze(
            state=state,
            request=request,
            tool_specs=[],
            tool_schemas=[{"type": "function", "function": {"name": "submit_review"}}],
        )
    )

    assert plan.draft_review is not None
    assert plan.draft_review.issues[0].severity.value == "warning"
    assert total_tokens == 24
    assert len(client.calls) == 2
    assert any(
        "summary mentions review concerns" in message.content
        for message in client.calls[1]
    )
