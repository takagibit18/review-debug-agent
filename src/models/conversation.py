"""Transient canonical transcript for provider-required tool conversations."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.models.schemas import Message


class AssistantToolTurn(BaseModel):
    """One original assistant message containing one or more tool calls."""

    model_config = ConfigDict(extra="forbid")

    response_id: str = ""
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ToolResultTurn(BaseModel):
    """One tool result paired with a call in the preceding assistant turn."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(..., min_length=1)
    content: str = ""


class ModelConversation:
    """RAM-only assistant/tool transcript scoped to one orchestrator run."""

    def __init__(self) -> None:
        self._turns: list[AssistantToolTurn | ToolResultTurn] = []
        self._pending: list[tuple[str, str]] = []

    @property
    def turns(self) -> tuple[AssistantToolTurn | ToolResultTurn, ...]:
        """Expose an immutable view for model-layer inspection and tests."""

        return tuple(self._turns)

    def clear(self) -> None:
        """Discard all transient provider state."""

        self._turns.clear()
        self._pending.clear()

    def add_assistant_tool_turn(
        self,
        *,
        response_id: str,
        content: str,
        thinking: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Retain one assistant boundary exactly, including all tool calls."""

        if not tool_calls:
            return
        normalized: list[dict[str, Any]] = []
        for index, raw_call in enumerate(tool_calls):
            call = raw_call
            call_id = str(call.get("id", "")).strip()
            if not call_id:
                call_id = f"runtime_{response_id or 'response'}_{index}"
                call["id"] = call_id
            function = call.get("function")
            name = (
                str(function.get("name", "")).strip()
                if isinstance(function, dict)
                else ""
            )
            normalized.append(call)
            self._pending.append((call_id, name))
        self._turns.append(
            AssistantToolTurn(
                response_id=response_id,
                content=content,
                thinking=thinking,
                tool_calls=normalized,
            )
        )

    def add_tool_result(self, tool_call_id: str, result: Any) -> None:
        """Append one result and mark its provider call as satisfied."""

        normalized_id = tool_call_id.strip()
        pending_ids = {call_id for call_id, _ in self._pending}
        if not normalized_id or normalized_id not in pending_ids:
            return
        self._turns.append(
            ToolResultTurn(
                tool_call_id=normalized_id,
                content=self._serialize_result(result),
            )
        )
        self._pending = [
            pair for pair in self._pending if pair[0] != normalized_id
        ]

    def add_tool_result_for_name(self, tool_name: str, result: Any) -> str:
        """Append a result for the earliest unresolved pseudo-tool with this name."""

        for call_id, name in self._pending:
            if name == tool_name:
                self.add_tool_result(call_id, result)
                return call_id
        return ""

    def messages(self) -> list[Message]:
        """Return canonical messages without provider-specific wire field names."""

        messages: list[Message] = []
        for turn in self._turns:
            if isinstance(turn, AssistantToolTurn):
                messages.append(
                    Message(
                        role="assistant",
                        content=turn.content,
                        tool_calls=turn.tool_calls,
                        thinking=turn.thinking or None,
                    )
                )
            else:
                messages.append(
                    Message(
                        role="tool",
                        content=turn.content,
                        tool_call_id=turn.tool_call_id,
                    )
                )
        return messages

    @staticmethod
    def _serialize_result(result: Any) -> str:
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        return json.dumps(result, ensure_ascii=True, sort_keys=True, default=str)
