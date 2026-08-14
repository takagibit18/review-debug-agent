"""Protocol regression tests for the transient model conversation."""

from __future__ import annotations

import json

from src.analyzer.schemas import AnalysisPlan
from src.models.conversation import (
    AssistantToolTurn,
    ModelConversation,
    ToolResultTurn,
)
from src.models.schemas import DraftFindingInput
from src.orchestrator.agent_loop import AgentOrchestrator
from src.orchestrator.run_journal import RunJournal


def _call(call_id: str, name: str) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_single_tool_turn_retains_thinking_for_replay() -> None:
    conversation = ModelConversation()
    conversation.add_assistant_tool_turn(
        response_id="response-1",
        content="",
        thinking="private chain",
        tool_calls=[_call("call-a", "read_file")],
    )
    conversation.add_tool_result("call-a", {"ok": True})

    messages = conversation.messages()
    assert [message.role for message in messages] == ["assistant", "tool"]
    assert messages[0].thinking == "private chain"
    assert messages[1].tool_call_id == "call-a"


def test_multi_tool_calls_preserve_one_assistant_boundary() -> None:
    conversation = ModelConversation()
    conversation.add_assistant_tool_turn(
        response_id="response-1",
        content="",
        thinking="one reasoning block",
        tool_calls=[_call("call-a", "read_file"), _call("call-b", "grep_code")],
    )
    conversation.add_tool_result("call-a", {"ok": True, "value": "a"})
    conversation.add_tool_result("call-b", {"ok": True, "value": "b"})

    turns = conversation.turns
    assert len(turns) == 3
    assert isinstance(turns[0], AssistantToolTurn)
    assert len(turns[0].tool_calls) == 2
    assert isinstance(turns[1], ToolResultTurn)
    assert isinstance(turns[2], ToolResultTurn)


def test_pseudo_tool_and_external_tool_receive_results_in_same_turn() -> None:
    conversation = ModelConversation()
    conversation.add_assistant_tool_turn(
        response_id="response-1",
        content="",
        thinking="draft then inspect",
        tool_calls=[
            _call("draft-call", "record_draft_finding"),
            _call("read-call", "read_file"),
        ],
    )
    pseudo_id = conversation.add_tool_result_for_name(
        "record_draft_finding",
        {"ok": True, "recorded": True, "draft_id": "df_123"},
    )
    conversation.add_tool_result("read-call", {"ok": True, "content": "value"})

    assert pseudo_id == "draft-call"
    messages = conversation.messages()
    assert [message.role for message in messages] == ["assistant", "tool", "tool"]
    assert json.loads(messages[1].content)["draft_id"] == "df_123"


def test_four_tool_rounds_are_not_windowed() -> None:
    conversation = ModelConversation()
    for index in range(4):
        call_id = f"call-{index}"
        conversation.add_assistant_tool_turn(
            response_id=f"response-{index}",
            content="",
            thinking=f"thinking-{index}",
            tool_calls=[_call(call_id, "read_file")],
        )
        conversation.add_tool_result(call_id, {"ok": True, "round": index})

    assert len(conversation.turns) == 8
    assert sum(isinstance(turn, AssistantToolTurn) for turn in conversation.turns) == 4


def test_orchestrator_persists_draft_before_synthetic_result(tmp_path) -> None:
    orchestrator = AgentOrchestrator()
    orchestrator._run_journal = RunJournal(  # noqa: SLF001
        "run-test", tmp_path / "journal.jsonl", fsync=False
    )
    orchestrator._model_conversation.add_assistant_tool_turn(  # noqa: SLF001
        response_id="provider-response",
        content="",
        thinking="private",
        tool_calls=[_call("draft-call", "record_draft_finding")],
    )
    plan = AnalysisPlan(
        source_response_id="rje-response",
        draft_finding_source_response_id="rje-response",
        draft_finding_calls=[
            DraftFindingInput(file="src/example.py", claim="Possible regression")
        ],
    )

    orchestrator._persist_draft_finding_calls(plan)  # noqa: SLF001

    entries = orchestrator._run_journal.replay()  # noqa: SLF001
    assert entries[-1].type == "draft_finding"
    result_turn = orchestrator._model_conversation.turns[-1]  # noqa: SLF001
    assert isinstance(result_turn, ToolResultTurn)
    assert json.loads(result_turn.content) == {
        "draft_id": entries[-1].payload["id"],
        "ok": True,
        "recorded": True,
    }


def test_orchestrator_run_reset_discards_conversation(tmp_path) -> None:
    orchestrator = AgentOrchestrator()
    orchestrator._model_conversation.add_assistant_tool_turn(  # noqa: SLF001
        response_id="old",
        content="",
        thinking="must not leak",
        tool_calls=[_call("old-call", "read_file")],
    )

    orchestrator._reset_run(1, str(tmp_path))  # noqa: SLF001

    assert orchestrator._model_conversation.turns == ()  # noqa: SLF001


def test_invalid_draft_pseudo_call_receives_validation_result() -> None:
    conversation = ModelConversation()
    raw_call = _call("invalid-draft", "record_draft_finding")
    conversation.add_assistant_tool_turn(
        response_id="response",
        content="",
        thinking="private",
        tool_calls=[raw_call],
    )
    from src.analyzer.inference_engine import InferenceEngine

    engine = InferenceEngine.__new__(InferenceEngine)
    engine._conversation = conversation  # noqa: SLF001
    engine._complete_invalid_draft_tool_calls(  # noqa: SLF001
        [raw_call], {"valid_draft_call_ids": []}
    )

    result = conversation.turns[-1]
    assert isinstance(result, ToolResultTurn)
    assert json.loads(result.content)["error_type"] == "validation_error"
