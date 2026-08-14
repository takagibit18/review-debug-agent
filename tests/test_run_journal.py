"""Tests for the durable append-only run journal."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.analyzer.context_state import ContextState
from src.analyzer.event_log import EventEntry, EventLog, EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import ReviewRequest
from src.models.schemas import ModelConfig, ModelResponse, TokenUsage
from src.orchestrator.agent_loop import AgentOrchestrator
from src.orchestrator.run_journal import (
    ModelResponseJournalPayload,
    PendingRunJournalEntry,
    RunJournal,
    RunJournalCorruptionError,
    RunJournalEntry,
    ToolResultJournalPayload,
)
from src.tools.base import BaseTool, ToolRegistry, ToolSpec


def _model_response_fact(
    *,
    finish_reason: str = "stop",
    content: str = "visible result",
) -> PendingRunJournalEntry:
    payload = ModelResponseJournalPayload(
        iteration=0,
        model="fake-model",
        finish_reason=finish_reason,
        content=content,
        tool_calls=[
            {
                "id": "call-1",
                "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
            }
        ],
        usage=TokenUsage(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        ),
    )
    return PendingRunJournalEntry(
        type="model_response",
        payload=payload.model_dump(mode="json"),
    )


def test_append_replay_and_last_entry_preserve_monotonic_order(tmp_path) -> None:
    journal = RunJournal("run-1", tmp_path / "journal.jsonl", fsync=False)

    first = journal.append(_model_response_fact())
    second = journal.append(_model_response_fact(content="second"))

    replayed = journal.replay()
    assert [entry.id for entry in replayed] == [first.id, second.id]
    assert [entry.seq for entry in replayed] == [1, 2]
    assert journal.last_entry() == second
    assert replayed[0].payload["tool_calls"][0]["id"] == "call-1"
    assert replayed[0].payload["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


def test_length_response_is_saved_without_reasoning_content(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = RunJournal("run-length", path, fsync=False)

    entry = journal.append(
        _model_response_fact(finish_reason="length", content="partial visible claim")
    )

    assert entry.payload["finish_reason"] == "length"
    assert entry.payload["content"] == "partial visible claim"
    assert "reasoning_content" not in path.read_text(encoding="utf-8")


def test_tool_result_follows_source_response_and_keeps_structured_result(
    tmp_path,
) -> None:
    journal = RunJournal("run-tools", tmp_path / "journal.jsonl", fsync=False)
    response = journal.append(_model_response_fact())
    tool_payload = ToolResultJournalPayload(
        source_response_id=response.id,
        tool_call_id="call-1",
        tool="read_file",
        arguments={"path": "x.py"},
        result={"ok": True, "data": {"content": "full structured body"}, "error": None},
    )

    tool_entry = journal.append(
        PendingRunJournalEntry(
            type="tool_result",
            payload=tool_payload.model_dump(mode="json"),
        )
    )

    assert response.seq < tool_entry.seq
    assert tool_entry.payload["source_response_id"] == response.id
    assert tool_entry.payload["result"]["data"] == {"content": "full structured body"}


def test_replay_ignores_only_a_malformed_final_non_empty_line(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = RunJournal("run-partial", path, fsync=False)
    valid = journal.append(_model_response_fact())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":"1.0","id":"partial')

    assert journal.replay() == [valid]

    recovered = RunJournal("run-partial", path, fsync=False)
    appended = recovered.append(_model_response_fact(content="after recovery"))
    assert [entry.seq for entry in recovered.replay()] == [1, 2]
    assert appended.payload["content"] == "after recovery"

    later = RunJournalEntry(
        seq=2,
        type="model_response",
        run_id="run-partial",
        payload=ModelResponseJournalPayload(
            iteration=1,
            model="fake-model",
            finish_reason="stop",
            content="later",
        ).model_dump(mode="json"),
    )
    path.write_text(
        valid.model_dump_json() + "\nnot-json\n" + later.model_dump_json() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RunJournalCorruptionError, match="line 2"):
        journal.replay()


def test_run_journal_is_independent_from_event_log(tmp_path) -> None:
    event_log = EventLog("run-independent", tmp_path / "logs")
    journal = RunJournal(
        "run-independent",
        tmp_path / "runs" / "run-independent" / "journal.jsonl",
        fsync=False,
    )
    event_log.record(
        EventEntry(
            run_id="run-independent",
            event_type=EventType.DECISION,
            phase="test",
            payload={"observable": True},
        )
    )
    journal.append(_model_response_fact())

    assert event_log.path != journal.path
    assert len(event_log.replay()) == 1
    assert len(journal.replay()) == 1


class _LengthModelClient:
    """Return one truncated response for persistence-order verification."""

    def __init__(self) -> None:
        self.default_config = ModelConfig(model="fake-model")

    async def chat(self, messages, config=None, tools=None, policy=None, conversation=None):  # type: ignore[no-untyped-def]
        return ModelResponse(
            content="visible partial finding",
            tool_calls=[],
            usage=TokenUsage(total_tokens=12),
            model="fake-model",
            finish_reason="length",
            reasoning_content="private reasoning must not be journaled",
        )


def test_model_response_is_persisted_before_tool_call_parsing(
    tmp_path, monkeypatch
) -> None:
    journal = RunJournal("run-order", tmp_path / "journal.jsonl", fsync=False)

    def _write_response(response: ModelResponse, iteration: int) -> str:
        payload = ModelResponseJournalPayload(
            iteration=iteration,
            model=response.model,
            finish_reason=response.finish_reason,
            content=response.content,
            tool_calls=response.tool_calls,
            usage=response.usage,
        )
        return journal.append(
            PendingRunJournalEntry(
                type="model_response",
                payload=payload.model_dump(mode="json"),
            )
        ).id

    engine = InferenceEngine(
        _LengthModelClient(),  # type: ignore[arg-type]
        model_response_writer=_write_response,
    )

    def _fail_parse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("parse failed after provider return")

    monkeypatch.setattr(engine, "_parse_tool_calls", _fail_parse)

    with pytest.raises(RuntimeError, match="parse failed"):
        asyncio.run(
            engine.analyze(
                state=ContextState(goal="review"),
                request=ReviewRequest(repo_path=str(tmp_path)),
                tool_specs=[],
            )
        )

    replayed = journal.replay()
    assert len(replayed) == 1
    assert replayed[0].payload["finish_reason"] == "length"
    serialized = json.dumps(replayed[0].model_dump(), ensure_ascii=False)
    assert "private reasoning" not in serialized


class _EchoTool(BaseTool):
    """Small readonly tool used by the journal integration test."""

    def spec(self) -> ToolSpec:
        return ToolSpec(name="echo", description="Echo input")

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        return {"echo": kwargs.get("value")}


class _ToolThenSubmitClient:
    """Request one tool, then produce an explicit empty review submit."""

    def __init__(self) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self.calls = 0

    async def chat(self, messages, config=None, tools=None, policy=None, conversation=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call-echo",
                        "function": {
                            "name": "echo",
                            "arguments": '{"value":"kept"}',
                        },
                    }
                ],
                usage=TokenUsage(total_tokens=5),
                model="fake-model",
                finish_reason="tool_calls",
            )
        return ModelResponse(
            tool_calls=[
                {
                    "id": "call-submit",
                    "function": {
                        "name": "submit_review",
                        "arguments": json.dumps(
                            {"summary": "No supported issues.", "issues": []}
                        ),
                    },
                }
            ],
            usage=TokenUsage(total_tokens=5),
            model="fake-model",
            finish_reason="tool_calls",
        )


def test_orchestrator_writes_model_then_tool_result_in_run_directory(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    orchestrator = AgentOrchestrator(registry=registry, review_max_iterations=2)
    orchestrator._model_client = _ToolThenSubmitClient()  # type: ignore[assignment]  # noqa: SLF001

    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    path = tmp_path / ".mergewarden" / "runs" / response.run_id / "journal.jsonl"
    replayed = RunJournal(response.run_id, path, fsync=False).replay()
    assert [entry.type for entry in replayed] == [
        "model_response",
        "tool_result",
        "model_response",
    ]
    assert replayed[1].payload["source_response_id"] == replayed[0].id
    assert replayed[1].payload["result"]["data"] == {"echo": "kept"}
